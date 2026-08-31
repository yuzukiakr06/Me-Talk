"""Me Talk DM(1対1)ルーター。

2人のユーザー間の会話。会話は(user_a,user_b)の組で一意。
会話開始/一覧・メッセージ取得/送信・既読を提供する。
送信時は相手へ WebSocket でリアルタイム配信する。

Dev yuzuki_akrdev.ofc
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session, select, or_, and_

from app.db import get_session
from app.models import Block, DMAttachmentView, DMConversation, DMMessage, MessageHidden, User
from app.realtime import manager
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/dm", tags=["dm"])

MAX_MESSAGE_LEN = 4000
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_TOTAL = 50 * 1024 * 1024


class StartConversation(BaseModel):
    user_id: int


class SendDM(BaseModel):
    content: str = ""
    reply_to: int | None = None
    attachments: list[dict] = []
    fade_mode: str | None = None
    fade_seconds: int | None = None


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _find_conversation(session: Session, a: int, b: int) -> DMConversation | None:
    lo, hi = _pair(a, b)
    return session.exec(
        select(DMConversation).where(DMConversation.user_a == lo, DMConversation.user_b == hi)
    ).first()


def _other_id(conv: DMConversation, me: int) -> int:
    return conv.user_b if conv.user_a == me else conv.user_a


def _expire_fades(session: Session, conv_id: int) -> list[int]:
    now_dt = datetime.utcnow()
    expired = session.exec(
        select(DMMessage).where(
            DMMessage.conversation_id == conv_id,
            DMMessage.deleted == False,  # noqa: E712
            DMMessage.fade_at != None,  # noqa: E711
        )
    ).all()
    ids = []
    for m in expired:
        if m.fade_at and m.fade_at <= now_dt:
            m.deleted = True
            session.add(m)
            ids.append(m.id)
    if ids:
        session.commit()
    return ids


def _require_conversation(session: Session, conv_id: int, me: int) -> DMConversation:
    conv = session.get(DMConversation, conv_id)
    if not conv or me not in (conv.user_a, conv.user_b):
        raise HTTPException(status_code=404, detail="会話が見つかりません。")
    return conv


def _is_blocked(session: Session, blocker: int, blocked: int) -> bool:
    """互換のために残す。判定は social に集約してあり、双方向で効く。"""
    from app import social
    return social.is_blocked(session, blocker, blocked)


def _conv_public(session: Session, conv: DMConversation, me: int) -> dict:
    other = session.get(User, _other_id(conv, me))
    last = session.exec(
        select(DMMessage).where(DMMessage.conversation_id == conv.id, DMMessage.deleted == False)  # noqa: E712
        .order_by(DMMessage.id.desc())
    ).first()
    unread = len(session.exec(
        select(DMMessage).where(
            DMMessage.conversation_id == conv.id,
            DMMessage.author_id != me,
            DMMessage.read_at == None,  # noqa: E711
            DMMessage.deleted == False,  # noqa: E712
        )
    ).all())
    return {
        "id": conv.id,
        "user": public_user(other) if other else None,
        "online": manager.is_online(other.id) if other else False,
        "last_message": (last.content or ("[添付]" if last.attachments and last.attachments != "[]" else "")) if last else "",
        "last_at": last.created_at.isoformat() if last else None,
        "unread": unread,
    }


def _dm_public(session: Session, msg: DMMessage, author: User | None = None, viewer_id: int | None = None) -> dict:
    if author is None:
        author = session.get(User, msg.author_id)
    try:
        attachments = json.loads(msg.attachments) if msg.attachments else []
    except (json.JSONDecodeError, TypeError):
        attachments = []
    if attachments and viewer_id is not None:
        views = session.exec(
            select(DMAttachmentView).where(DMAttachmentView.message_id == msg.id)
        ).all()
        viewed_idx = {v.attachment_index for v in views if v.viewer_id == viewer_id}
        is_recipient = viewer_id != msg.author_id
        for i, att in enumerate(attachments):
            if att.get("fade_view"):
                att["fade_view"] = True
                if is_recipient and i in viewed_idx:
                    att["consumed"] = True
                    att["url"] = ""
                elif not is_recipient:
                    att["consumed"] = i in {v.attachment_index for v in views if v.viewer_id != msg.author_id}
    return {
        "id": msg.id,
        "conversation_id": msg.conversation_id,
        "content": msg.content,
        "reply_to": msg.reply_to,
        "attachments": attachments,
        "deleted": msg.deleted,
        "created_at": msg.created_at.isoformat(),
        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
        "read_at": msg.read_at.isoformat() if msg.read_at else None,
        "fade_mode": msg.fade_mode,
        "fade_seconds": msg.fade_seconds,
        "fade_at": msg.fade_at.isoformat() if msg.fade_at else None,
        "author": public_user(author) if author else None,
    }


@router.get("/conversations")
def list_conversations(user: User = Depends(current_user), session: Session = Depends(get_session)):
    convs = session.exec(
        select(DMConversation).where(
            or_(DMConversation.user_a == user.id, DMConversation.user_b == user.id)
        )
    ).all()
    result = [_conv_public(session, c, user.id) for c in convs]
    result.sort(key=lambda x: x["last_at"] or "", reverse=True)
    return {"conversations": result}


@router.post("/conversations")
def start_conversation(payload: StartConversation, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if payload.user_id == user.id:
        raise HTTPException(status_code=400, detail="自分自身とは会話できません。")
    other = session.get(User, payload.user_id)
    if not other or not other.is_active:
        raise HTTPException(status_code=404, detail="相手が見つかりません。")
    from app import social
    ok, reason = social.can_direct_message(session, user, other)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)
    conv = _find_conversation(session, user.id, other.id)
    if not conv:
        lo, hi = _pair(user.id, other.id)
        conv = DMConversation(user_a=lo, user_b=hi)
        session.add(conv)
        session.commit()
        session.refresh(conv)
    return _conv_public(session, conv, user.id)


@router.get("/conversations/{conv_id}/messages")
def get_dm_messages(
    conv_id: int,
    before: int | None = Query(default=None),
    limit: int = Query(default=50, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_conversation(session, conv_id, user.id)
    _expire_fades(session, conv_id)
    query = select(DMMessage).where(DMMessage.conversation_id == conv_id, DMMessage.deleted == False)  # noqa: E712
    if before:
        query = query.where(DMMessage.id < before)
    query = query.order_by(DMMessage.id.desc()).limit(limit)
    messages = session.exec(query).all()
    hidden = {
        h.message_id for h in session.exec(
            select(MessageHidden).where(MessageHidden.user_id == user.id, MessageHidden.scope == "dm")
        ).all()
    }
    messages = [m for m in messages if m.id not in hidden]
    messages.reverse()
    from app.routes_reactions import reactions_for
    react_map = reactions_for(session, "dm", [m.id for m in messages], user.id)
    result = []
    for m in messages:
        d = _dm_public(session, m, viewer_id=user.id)
        d["reactions"] = react_map.get(m.id, [])
        result.append(d)
    return {"messages": result}


@router.post("/conversations/{conv_id}/messages")
async def send_dm(
    conv_id: int,
    payload: SendDM,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    conv = _require_conversation(session, conv_id, user.id)
    other_id = _other_id(conv, user.id)
    from app import social
    other_user = session.get(User, other_id)
    ok, reason = social.can_direct_message(session, user, other_user)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)

    is_app_client = request.headers.get("X-Me-Talk-Client", "").lower() == "app"

    content = payload.content.strip()
    attachments = payload.attachments or []
    if not content and not attachments:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    if len(content) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"メッセージは{MAX_MESSAGE_LEN}文字以内にしてください。")
    if len(attachments) > MAX_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"添付は{MAX_ATTACHMENTS}件までです。")

    clean_attachments = []
    total = 0
    for a in attachments:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url", ""))[:500]
        if not url:
            continue
        size = int(a.get("size", 0)) if str(a.get("size", "")).isdigit() else 0
        total += size
        fade_view = bool(a.get("fade_view", False)) and is_app_client
        clean_attachments.append({
            "kind": str(a.get("kind", "file"))[:16],
            "url": url,
            "name": str(a.get("name", ""))[:120],
            "content_type": str(a.get("content_type", ""))[:80],
            "size": size,
            "fade_view": fade_view,
            "fade_seconds": (int(a.get("fade_seconds", 0)) if str(a.get("fade_seconds", "")).isdigit() else 0) if fade_view else 0,
        })
    if total > MAX_ATTACHMENT_TOTAL:
        raise HTTPException(status_code=400, detail="添付の合計サイズが上限を超えています(1メッセージあたり50MBまで)。")

    fade_mode = payload.fade_mode if payload.fade_mode in ("view", "timer") else None
    fade_seconds = None
    fade_at = None
    if fade_mode:
        fade_seconds = payload.fade_seconds if payload.fade_seconds and payload.fade_seconds > 0 else 3600
        fade_seconds = min(fade_seconds, 30 * 24 * 3600)
        if fade_mode == "timer":
            fade_at = datetime.utcnow() + timedelta(seconds=fade_seconds)

    msg = DMMessage(
        conversation_id=conv_id,
        author_id=user.id,
        content=content,
        reply_to=payload.reply_to,
        attachments=json.dumps(clean_attachments, ensure_ascii=False),
        fade_mode=fade_mode,
        fade_seconds=fade_seconds,
        fade_at=fade_at,
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)

    data = _dm_public(session, msg, author=user)
    await manager.send_to_users([user.id, other_id], {"type": "message.dm", "data": data})
    from app.routes_notifications import create_notification
    import re as _re
    if content and _re.match(r"^\[stamp:[a-z_]+\]$", content):
        preview = "スタンプを送信しました"
    else:
        preview = content[:60] if content else "[画像を送信しました]"
    await create_notification(
        session, other_id, "dm", f"{user.username} さんからのメッセージ", preview,
        actor_id=user.id, target_type="dm", target_id=conv_id,
    )
    return data


@router.post("/conversations/{conv_id}/read")
def mark_read(conv_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_conversation(session, conv_id, user.id)
    unread = session.exec(
        select(DMMessage).where(
            DMMessage.conversation_id == conv_id,
            DMMessage.author_id != user.id,
            DMMessage.read_at == None,  # noqa: E711
        )
    ).all()
    now_dt = datetime.utcnow()
    faded = []
    for m in unread:
        m.read_at = now_dt
        if m.fade_mode == "view" and m.fade_at is None:
            m.fade_at = now_dt + timedelta(seconds=m.fade_seconds or 3600)
            faded.append(m.id)
        session.add(m)
    session.commit()
    return {"read": len(unread), "fade_started": faded}


@router.get("/users/search")
def search_users(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, le=50),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="検索キーワードを入力してください。")
    users = session.exec(
        select(User).where(User.username.contains(keyword), User.is_active == True)  # noqa: E712
        .limit(limit)
    ).all()
    result = []
    for u in users:
        if u.id == user.id:
            continue
        entry = public_user(u)
        entry["online"] = manager.is_online(u.id)
        result.append(entry)
    return {"users": result}


class DMMessageIds(BaseModel):
    message_ids: list[int]


DM_UNSEND_WINDOW = timedelta(hours=24)


@router.post("/conversations/{conv_id}/messages/hide")
def hide_dm_messages(
    conv_id: int,
    payload: DMMessageIds,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_conversation(session, conv_id, user.id)
    count = 0
    for mid in payload.message_ids[:100]:
        exists = session.exec(
            select(MessageHidden).where(
                MessageHidden.user_id == user.id,
                MessageHidden.scope == "dm",
                MessageHidden.message_id == mid,
            )
        ).first()
        if not exists:
            session.add(MessageHidden(user_id=user.id, scope="dm", message_id=mid))
            count += 1
    session.commit()
    return {"hidden": count}


@router.post("/conversations/{conv_id}/messages/{message_id}/unsend")
async def unsend_dm_message(
    conv_id: int,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    conv = _require_conversation(session, conv_id, user.id)
    msg = session.get(DMMessage, message_id)
    if not msg or msg.conversation_id != conv_id:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="自分のメッセージのみ取り消せます。")
    if datetime.utcnow() - msg.created_at > DM_UNSEND_WINDOW:
        raise HTTPException(status_code=400, detail="送信から24時間を過ぎたメッセージは取り消せません。")
    msg.deleted = True
    session.add(msg)
    session.commit()
    other_id = _other_id(conv, user.id)
    await manager.send_to_users(
        [user.id, other_id],
        {"type": "message.unsend", "data": {"conversation_id": conv_id, "message_id": message_id, "scope": "dm"}},
    )
    return {"unsent": True}


DM_EDIT_WINDOW = timedelta(hours=1)


class EditDM(BaseModel):
    content: str


@router.patch("/conversations/{conv_id}/messages/{message_id}")
async def edit_dm_message(
    conv_id: int,
    message_id: int,
    payload: EditDM,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    conv = _require_conversation(session, conv_id, user.id)
    msg = session.get(DMMessage, message_id)
    if not msg or msg.conversation_id != conv_id or msg.deleted:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="自分のメッセージのみ編集できます。")
    if datetime.utcnow() - msg.created_at > DM_EDIT_WINDOW:
        raise HTTPException(status_code=400, detail="送信から1時間を過ぎたメッセージは編集できません。")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    if len(content) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"メッセージは{MAX_MESSAGE_LEN}文字以内にしてください。")
    msg.content = content
    msg.edited_at = datetime.utcnow()
    session.add(msg)
    session.commit()
    session.refresh(msg)
    data = _dm_public(session, msg, author=user)
    other_id = _other_id(conv, user.id)
    await manager.send_to_users([user.id, other_id], {"type": "message.edit", "data": data})
    return data


class ViewAttachment(BaseModel):
    attachment_index: int = 0


@router.post("/conversations/{conv_id}/messages/{message_id}/view-attachment")
async def view_attachment(
    conv_id: int,
    message_id: int,
    payload: ViewAttachment,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    if request.headers.get("X-Me-Talk-Client", "").lower() != "app":
        raise HTTPException(status_code=403, detail="消える画像はアプリ版でのみ表示できます。")
    conv = _require_conversation(session, conv_id, user.id)
    msg = session.get(DMMessage, message_id)
    if not msg or msg.conversation_id != conv_id or msg.deleted:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    if msg.author_id == user.id:
        return {"viewed": False, "reason": "自分の送信した画像です。"}
    try:
        attachments = json.loads(msg.attachments) if msg.attachments else []
    except (json.JSONDecodeError, TypeError):
        attachments = []
    idx = payload.attachment_index
    if idx < 0 or idx >= len(attachments):
        raise HTTPException(status_code=400, detail="添付が見つかりません。")
    att = attachments[idx]
    if not att.get("fade_view"):
        return {"viewed": False, "reason": "この添付は消える画像ではありません。"}
    existing = session.exec(
        select(DMAttachmentView).where(
            DMAttachmentView.message_id == message_id,
            DMAttachmentView.attachment_index == idx,
            DMAttachmentView.viewer_id == user.id,
        )
    ).first()
    if existing:
        return {"viewed": True, "already": True}
    session.add(DMAttachmentView(message_id=message_id, attachment_index=idx, viewer_id=user.id))
    session.commit()
    other_id = _other_id(conv, user.id)
    await manager.send_to_users(
        [other_id],
        {"type": "attachment.viewed", "data": {"conversation_id": conv_id, "message_id": message_id, "attachment_index": idx, "viewer": user.username}},
    )
    return {"viewed": True}


class ScreenshotNotice(BaseModel):
    message_id: int | None = None


@router.post("/conversations/{conv_id}/screenshot")
async def notify_screenshot(
    conv_id: int,
    payload: ScreenshotNotice,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    conv = _require_conversation(session, conv_id, user.id)
    other_id = _other_id(conv, user.id)
    await manager.send_to_users(
        [other_id, user.id],
        {"type": "screenshot.detected", "data": {"conversation_id": conv_id, "by": user.username, "message_id": payload.message_id}},
    )
    return {"notified": True}
