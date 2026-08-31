"""Me Talk 2段階認証ユーティリティ。

認証アプリ(Google Authenticator など)が使う TOTP を RFC 6238 に沿って実装する。
外部サービスに依存せず、標準ライブラリの hmac と hashlib だけで完結する。
QRコードは qrcode の行列を自前でSVGに組み立てるため画像ライブラリを必要としない。

Dev yuzuki_akrdev.ofc
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
WINDOW = 1
SECRET_BYTES = 20
BACKUP_CODE_COUNT = 10
BACKUP_GROUP = 4


def make_secret() -> str:
    """認証アプリに登録する共有鍵をBase32で作る。"""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _code_at(secret: str, counter: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, code: str, at: int | None = None) -> bool:
    """入力されたコードが正しいか確かめる。時計のずれを前後1枠だけ許容する。"""
    if not secret or not code:
        return False
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) != DIGITS:
        return False
    now = int(at if at is not None else time.time())
    counter = now // PERIOD
    for drift in range(-WINDOW, WINDOW + 1):
        try:
            expected = _code_at(secret, counter + drift)
        except Exception:
            return False
        if hmac.compare_digest(expected, cleaned):
            return True
    return False


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """認証アプリが読み取る otpauth URI を組み立てる。"""
    label = quote(f"{issuer}:{account}", safe="")
    params = "&".join([
        f"secret={secret}",
        f"issuer={quote(issuer, safe='')}",
        f"algorithm=SHA1",
        f"digits={DIGITS}",
        f"period={PERIOD}",
    ])
    return f"otpauth://totp/{label}?{params}"


def qr_svg(data: str, scale: int = 6) -> str:
    """QRコードをSVG文字列として返す。"""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    size = len(matrix)
    side = size * scale
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" width="{side}" height="{side}" shape-rendering="crispEdges">',
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>',
    ]
    for y, row in enumerate(matrix):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            run = x
            while run < size and row[run]:
                run += 1
            parts.append(
                f'<rect x="{x * scale}" y="{y * scale}" width="{(run - x) * scale}" height="{scale}" fill="#000000"/>'
            )
            x = run
    parts.append("</svg>")
    return "".join(parts)


def format_secret(secret: str) -> str:
    """手入力しやすいように共有鍵を4文字ずつ区切る。"""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def make_backup_codes(count: int = BACKUP_CODE_COUNT) -> list:
    """バックアップコードを作る。読み違えやすい文字は使わない。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    while len(codes) < count:
        raw = "".join(secrets.choice(alphabet) for _ in range(BACKUP_GROUP * 2))
        code = f"{raw[:BACKUP_GROUP]}-{raw[BACKUP_GROUP:]}"
        if code not in codes:
            codes.append(code)
    return codes


def normalize_backup_code(code: str) -> str:
    """入力されたバックアップコードを比較できる形に整える。"""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def backup_hash(code: str) -> str:
    return hashlib.sha256(normalize_backup_code(code).encode("utf-8")).hexdigest()
