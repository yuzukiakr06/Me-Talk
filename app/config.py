"""MIRAI Chat 設定読み込み。

config.json があればそれを優先し、無ければ環境変数を使う。
MIRAI ID と同じ SMTP 設定形式に合わせている。

Dev yuzuki_akrdev.ofc
"""

import json
import os
from pathlib import Path
from functools import lru_cache

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"


@lru_cache
def _raw_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def setting(key: str, default: str = "") -> str:
    value = _raw_config().get(key)
    if value is not None and str(value).strip() != "":
        return str(value)
    env_value = os.getenv(key)
    if env_value is not None and env_value.strip() != "":
        return env_value
    return default


def setting_int(key: str, default: int) -> int:
    value = setting(key, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def setting_bool(key: str, default: bool = False) -> bool:
    value = setting(key, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DATABASE_URL = setting("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'mirai_chat.db'}")
SITE_NAME = setting("SITE_NAME", "MIRAI Chat")
APP_VERSION = setting("APP_VERSION", "v0.1.0 (Tester)")
DEVELOPER_USERNAMES = [u.strip() for u in setting("DEVELOPER_USERNAMES", "Yuzuki06_ofc").split(",") if u.strip()]
TESTER_BADGE_ENABLED = setting_bool("TESTER_BADGE_ENABLED", True)
USERNAME_MIN_LENGTH = setting_int("USERNAME_MIN_LENGTH", 3)
USERNAME_MAX_LENGTH = setting_int("USERNAME_MAX_LENGTH", 16)


def _parse_renames(raw: str) -> list:
    """「旧名:新名」をカンマ区切りで並べた設定を組のリストに変換する。"""
    pairs = []
    for item in raw.split(","):
        if ":" not in item:
            continue
        old, new = item.split(":", 1)
        if old.strip() and new.strip():
            pairs.append((old.strip(), new.strip()))
    return pairs


USERNAME_RENAMES = _parse_renames(setting("USERNAME_RENAMES", ""))
BASE_URL = setting("BASE_URL", "http://localhost:8000")

SMTP_HOST = setting("SMTP_HOST")
SMTP_PORT = setting_int("SMTP_PORT", 587)
SMTP_SECURE = setting("SMTP_SECURE", "starttls")
SMTP_USER = setting("SMTP_USER")
SMTP_PASSWORD = setting("SMTP_PASSWORD")
SMTP_FROM = setting("SMTP_FROM", SMTP_USER or "no-reply@mirai-vps.jp")

GOOGLE_CLIENT_ID = setting("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = setting("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = setting("GOOGLE_REDIRECT_URI", f"{BASE_URL}/api/auth/google/callback")
TURNSTILE_SITE_KEY = setting("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = setting("TURNSTILE_SECRET_KEY")

SUPPORT_EMAIL = setting("SUPPORT_EMAIL", "miraiserver.info@gmail.com")
ENCRYPTION_KEY = setting("ENCRYPTION_KEY", "")
STUN_URLS = setting("STUN_URLS", "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302")
TURN_URLS = setting("TURN_URLS", "")
TURN_USERNAME = setting("TURN_USERNAME", "")
TURN_PASSWORD = setting("TURN_PASSWORD", "")
TURN_KEY_ID = setting("TURN_KEY_ID", "")
TURN_KEY_API_TOKEN = setting("TURN_KEY_API_TOKEN", "")
TURN_TTL_SECONDS = setting_int("TURN_TTL_SECONDS", 86400)
CALL_MAX_PARTICIPANTS = setting_int("CALL_MAX_PARTICIPANTS", 4)
SFU_APP_ID = setting("SFU_APP_ID", "")
SFU_APP_SECRET = setting("SFU_APP_SECRET", "")
SFU_MAX_PARTICIPANTS = setting_int("SFU_MAX_PARTICIPANTS", 25)


def _default_rp_id() -> str:
    """BASE_URL からパスキーの適用ドメインを取り出す。"""
    from urllib.parse import urlparse
    host = urlparse(BASE_URL).hostname or "localhost"
    return host


PASSKEY_RP_ID = setting("PASSKEY_RP_ID", _default_rp_id())
PASSKEY_RP_NAME = setting("PASSKEY_RP_NAME", SITE_NAME)
PASSKEY_ORIGIN = setting("PASSKEY_ORIGIN", BASE_URL.rstrip("/"))
SESSION_COOKIE_NAME = setting("SESSION_COOKIE_NAME", "mchat_session")
HIDE_DEV_CODES = setting_bool("HIDE_DEV_CODES", False)

ANTHROPIC_API_KEY = setting("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = setting("ANTHROPIC_MODEL", "claude-sonnet-4-5")
AI_ASSISTANT_NAME = setting("AI_ASSISTANT_NAME", "Mirei agent")
