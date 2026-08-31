"""Me Talk リアクションルーター。

オープンチャット・DMのメッセージに任意の絵文字でリアクションできる。
同じ絵文字を再度押すとトグルで外れる。集計はメッセージ取得側で埋め込むための
ヘルパ reactions_for() を提供し、WebSocket でリアルタイム更新も配信する。

Dev yuzuki_akrdev.ofc
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import (
    DMConversation,
    DMMessage,
    MessageReaction,
    OpenChatMember,
    OpenChatMessage,
    User,
)
from app.realtime import manager
from app.routes_auth import current_user

router = APIRouter(prefix="/api/reactions", tags=["reactions"])

MAX_EMOJI_LEN = 16


class ReactionPayload(BaseModel):
    scope: str
    message_id: int
    emoji: str


def reactions_for(session: Session, scope: str, message_ids: list[int], me: int) -> dict:
    """message_id -> [{emoji, count, mine}] の集計を返す。"""
    if not message_ids:
        return {}
    rows = session.exec(
        select(MessageReaction).where(
            MessageReaction.scope == scope,
            MessageReaction.message_id.in_(message_ids),
        )
    ).all()
    grouped: dict[int, dict[str, dict]] = {}
    for r in rows:
        by_emoji = grouped.setdefault(r.message_id, {})
        entry = by_emoji.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "mine": False})
        entry["count"] += 1
        if r.user_id == me:
            entry["mine"] = True
    return {mid: list(emojis.values()) for mid, emojis in grouped.items()}


def _check_access(session: Session, scope: str, message_id: int, user: User) -> list[int]:
    """メッセージへのアクセス権を確認し、WS配信先ユーザーIDリストを返す。"""
    if scope == "openchat":
        msg = session.get(OpenChatMessage, message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
        member = session.exec(
            select(OpenChatMember).where(
                OpenChatMember.openchat_id == msg.openchat_id,
                OpenChatMember.user_id == user.id,
            )
        ).first()
        if not member:
            raise HTTPException(status_code=403, detail="このグループのメンバーではありません。")
        members = session.exec(
            select(OpenChatMember.user_id).where(OpenChatMember.openchat_id == msg.openchat_id)
        ).all()
        return [m for m in members], msg.openchat_id
    else:
        msg = session.get(DMMessage, message_id)
        if not msg:
            raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
        conv = session.get(DMConversation, msg.conversation_id)
        if not conv or user.id not in (conv.user_a, conv.user_b):
            raise HTTPException(status_code=403, detail="この会話にアクセスできません。")
        return [conv.user_a, conv.user_b], msg.conversation_id


@router.post("/toggle")
async def toggle_reaction(
    payload: ReactionPayload,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    scope = payload.scope if payload.scope in ("openchat", "dm") else "openchat"
    emoji = (payload.emoji or "").strip()[:MAX_EMOJI_LEN]
    if not emoji:
        raise HTTPException(status_code=400, detail="絵文字が空です。")
    targets, container_id = _check_access(session, scope, payload.message_id, user)
    existing = session.exec(
        select(MessageReaction).where(
            MessageReaction.scope == scope,
            MessageReaction.message_id == payload.message_id,
            MessageReaction.user_id == user.id,
            MessageReaction.emoji == emoji,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
        added = False
    else:
        session.add(MessageReaction(scope=scope, message_id=payload.message_id, user_id=user.id, emoji=emoji))
        session.commit()
        added = True
    summary = reactions_for(session, scope, [payload.message_id], user.id).get(payload.message_id, [])
    await manager.send_to_users(
        targets,
        {"type": "reaction.update", "data": {"scope": scope, "message_id": payload.message_id, "container_id": container_id, "reactions": summary}},
    )
    return {"added": added, "reactions": summary}


@router.get("/{scope}/{message_id}")
def get_reactions(
    scope: str,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    scope = scope if scope in ("openchat", "dm") else "openchat"
    _check_access(session, scope, message_id, user)
    return {"reactions": reactions_for(session, scope, [message_id], user.id).get(message_id, [])}
