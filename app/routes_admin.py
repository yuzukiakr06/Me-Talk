"""Me Talk 管理者API。

開発者バッジを持つユーザーだけが利用できる。
バッジの付与・剥奪と、対象ユーザーの検索を提供する。

Dev yuzuki_akrdev.ofc
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app import badges as badge_util
from app.db import get_session
from app.models import User
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_developer(user: User = Depends(current_user)) -> User:
    """開発者バッジがなければ 403 を返す。"""
    if not badge_util.is_developer(user):
        raise HTTPException(status_code=403, detail="この操作には管理者権限が必要です。")
    return user


class BadgePayload(BaseModel):
    user_id: int
    badge: str


@router.get("/badges")
def list_badges(admin: User = Depends(require_developer)):
    return {
        "badges": [
            {"key": key, "label": badge_util.BADGE_LABELS.get(key, key)}
            for key in badge_util.BADGE_KEYS
        ]
    }


@router.get("/users")
def search_users(
    q: str = Query(default="", max_length=40),
    limit: int = Query(default=30, le=100),
    admin: User = Depends(require_developer),
    session: Session = Depends(get_session),
):
    query = select(User)
    if q:
        query = query.where(User.username.contains(q))
    users = session.exec(query.limit(limit)).all()
    return {
        "users": [
            {
                "id": u.id,
                "user_id": u.user_id or "",
                "username": u.username,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "badges": badge_util.badge_list(u),
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ]
    }


@router.post("/badges/grant")
def grant_badge(
    payload: BadgePayload,
    admin: User = Depends(require_developer),
    session: Session = Depends(get_session),
):
    if payload.badge not in badge_util.BADGE_KEYS:
        raise HTTPException(status_code=400, detail="そのバッジは存在しません。")
    target = session.get(User, payload.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    changed = badge_util.add_badge(target, payload.badge)
    if changed:
        session.add(target)
        session.commit()
        session.refresh(target)
    return {"changed": changed, "badges": badge_util.badge_list(target)}


@router.post("/badges/revoke")
def revoke_badge(
    payload: BadgePayload,
    admin: User = Depends(require_developer),
    session: Session = Depends(get_session),
):
    if payload.badge not in badge_util.BADGE_KEYS:
        raise HTTPException(status_code=400, detail="そのバッジは存在しません。")
    target = session.get(User, payload.user_id)
    if not target:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    if payload.badge == "developer" and target.id == admin.id:
        raise HTTPException(status_code=400, detail="自分の開発者バッジは外せません。")
    changed = badge_util.remove_badge(target, payload.badge)
    if changed:
        session.add(target)
        session.commit()
        session.refresh(target)
    return {"changed": changed, "badges": badge_util.badge_list(target)}


@router.get("/me")
def admin_me(admin: User = Depends(require_developer)):
    data = public_user(admin)
    data["is_admin"] = True
    return data


@router.get("/anonymous/{message_id}")
def reveal_anonymous(message_id: int, admin: User = Depends(require_developer), session: Session = Depends(get_session)):
    """匿名投稿の実際の投稿者を確認する。違反対応のための機能。

    グループのオーナーや他の参加者からは辿れない。開発者のみが使える。
    """
    from app.models import OpenChat, OpenChatMessage
    msg = session.get(OpenChatMessage, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="そのメッセージは見つかりません。")
    if not msg.anonymous:
        raise HTTPException(status_code=400, detail="このメッセージは匿名投稿ではありません。")
    author = session.get(User, msg.author_id)
    oc = session.get(OpenChat, msg.openchat_id)
    return {
        "message_id": msg.id,
        "openchat": {"id": msg.openchat_id, "name": oc.name if oc else ""},
        "anon_label": msg.anon_label,
        "content": msg.content,
        "created_at": msg.created_at.isoformat(),
        "author": public_user(author, admin) if author else None,
    }
