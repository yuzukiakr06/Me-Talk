"""Me Talk バッジ管理。

バッジの定義と、ユーザーへの付与・剥奪・起動時同期を担当する。
バッジは User.badges にカンマ区切りで保存する。

Dev yuzuki_akrdev.ofc
"""

from sqlmodel import Session, select

from app import config
from app.models import User

BADGE_KEYS = ["developer", "tester"]

BADGE_LABELS = {
    "developer": "開発者",
    "tester": "早期サポーター (テスター)",
}


def badge_list(user: User) -> list:
    """ユーザーのバッジをリストで返す。"""
    return [b for b in (user.badges or "").split(",") if b]


def set_badges(user: User, badges: list) -> None:
    """バッジを設定する。未知のキーは無視し、定義順に並べる。"""
    cleaned = [b for b in BADGE_KEYS if b in badges]
    user.badges = ",".join(cleaned)


def add_badge(user: User, key: str) -> bool:
    """バッジを1つ付与する。付与できたら True。"""
    if key not in BADGE_KEYS:
        return False
    current = badge_list(user)
    if key in current:
        return False
    current.append(key)
    set_badges(user, current)
    return True


def remove_badge(user: User, key: str) -> bool:
    """バッジを1つ剥奪する。剥奪できたら True。"""
    current = badge_list(user)
    if key not in current:
        return False
    current.remove(key)
    set_badges(user, current)
    return True


def is_developer(user: User) -> bool:
    """開発者バッジを持っているかどうか。"""
    return "developer" in badge_list(user)


def initial_badges(username: str) -> list:
    """新規登録時に付与するバッジを決める。"""
    badges = []
    if config.TESTER_BADGE_ENABLED:
        badges.append("tester")
    if _is_configured_developer(username):
        badges.append("developer")
    return [b for b in BADGE_KEYS if b in badges]


def _is_configured_developer(username: str) -> bool:
    lowered = (username or "").lower()
    return any(lowered == name.lower() for name in config.DEVELOPER_USERNAMES)


def sync_developer_badges(session: Session) -> dict:
    """設定の DEVELOPER_USERNAMES と実データを同期する。

    設定に載っているユーザーには開発者バッジを付け、
    載っていないのに持っている場合は剥奪する。
    """
    granted = []
    revoked = []
    users = session.exec(select(User)).all()
    for user in users:
        should = _is_configured_developer(user.username)
        has = is_developer(user)
        if should and not has:
            add_badge(user, "developer")
            session.add(user)
            granted.append(user.username)
        elif has and not should:
            remove_badge(user, "developer")
            session.add(user)
            revoked.append(user.username)
    if granted or revoked:
        session.commit()
    return {"granted": granted, "revoked": revoked}
