"""Me Talk 通知センタールーター。

メンション・DM・フレンド申請・グループ招待などの通知を保存し、一覧・未読数・
既読化を提供する。他モジュールからは create_notification() を呼んで通知を作成する。
作成時は WebSocket でリアルタイムにも配信する。

Dev yuzuki_akrdev.ofc
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Notification, User
from app.realtime import manager
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

MAX_LIST = 100


async def create_notification(
    session: Session,
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
) -> Notification:
    """通知を1件作成し、対象ユーザーへWSでも配信する。"""
    if actor_id is not None and actor_id == user_id:
        return None
    notif = Notification(
        user_id=user_id,
        kind=kind,
        title=title[:120],
        body=body[:300],
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )
    session.add(notif)
    session.commit()
    session.refresh(notif)
    actor = session.get(User, actor_id) if actor_id else None
    await manager.send_to_users(
        [user_id],
        {"type": "notification.new", "data": _public(notif, actor)},
    )
    return notif


def create_notification_sync(
    session: Session,
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
) -> Notification:
    """同期コンテキスト用。通知を保存する(WS配信は行わない)。"""
    if actor_id is not None and actor_id == user_id:
        return None
    notif = Notification(
        user_id=user_id,
        kind=kind,
        title=title[:120],
        body=body[:300],
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )
    session.add(notif)
    session.commit()
    session.refresh(notif)
    return notif


def _public(n: Notification, actor: User | None = None) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "actor": public_user(actor) if actor else None,
        "target_type": n.target_type,
        "target_id": n.target_id,
        "read": n.read,
        "created_at": n.created_at.isoformat(),
    }


@router.get("")
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, le=MAX_LIST),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read == False)  # noqa: E712
    query = query.order_by(Notification.id.desc()).limit(limit)
    notifs = session.exec(query).all()
    actors = {}
    for n in notifs:
        if n.actor_id and n.actor_id not in actors:
            actors[n.actor_id] = session.get(User, n.actor_id)
    return {"notifications": [_public(n, actors.get(n.actor_id)) for n in notifs]}


@router.get("/unread-count")
def unread_count(user: User = Depends(current_user), session: Session = Depends(get_session)):
    count = len(session.exec(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.read == False,  # noqa: E712
        )
    ).all())
    return {"count": count}


class ReadPayload(BaseModel):
    notification_ids: list[int] = []


@router.post("/read")
def mark_read(payload: ReadPayload, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not payload.notification_ids:
        return {"read": 0}
    notifs = session.exec(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.id.in_(payload.notification_ids),
        )
    ).all()
    for n in notifs:
        n.read = True
        session.add(n)
    session.commit()
    return {"read": len(notifs)}


@router.post("/read-all")
def mark_all_read(user: User = Depends(current_user), session: Session = Depends(get_session)):
    notifs = session.exec(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.read == False,  # noqa: E712
        )
    ).all()
    for n in notifs:
        n.read = True
        session.add(n)
    session.commit()
    return {"read": len(notifs)}
