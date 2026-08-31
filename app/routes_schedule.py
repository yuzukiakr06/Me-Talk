"""Me Talk 予約送信・リマインド。

指定した時刻にサーバーが自動でメッセージを投稿する仕組みと、
指定した時刻に本人へ知らせるリマインドを担当する。

時刻の判定は20秒ごとのバックグラウンドワーカーが行う。
アプリを閉じていてもサーバー側で実行されるため、端末の状態に左右されない。

Dev yuzuki_akrdev.ofc
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import engine, get_session
from app.models import (
    DMConversation,
    DMMessage,
    OpenChat,
    OpenChatMember,
    OpenChatMessage,
    Reminder,
    ScheduledMessage,
    User,
)
from app.realtime import manager
from app.routes_auth import current_user

router = APIRouter(prefix="/api/schedule", tags=["schedule"])

TICK_SECONDS = 20
MAX_CONTENT = 4000
MAX_PENDING = 50
MIN_LEAD_SECONDS = 30
MAX_AHEAD_DAYS = 365


class ScheduleCreate(BaseModel):
    scope: str
    target_id: int
    content: str
    send_at: str


class ReminderCreate(BaseModel):
    scope: str = "openchat"
    target_id: int | None = None
    text: str
    remind_at: str
    post_in_chat: bool = True
    source_message_id: int | None = None


def parse_time(value: str) -> datetime:
    """クライアントから来た時刻をUTCの naive datetime に直す。

    タイムゾーン付きで送られてくる前提だが、
    付いていない場合はUTCとして扱う。
    """
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="日時を指定してください。")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="日時の形式が正しくありません。")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def validate_when(when: datetime) -> datetime:
    now = datetime.utcnow()
    if when < now + timedelta(seconds=MIN_LEAD_SECONDS):
        raise HTTPException(status_code=400, detail="現在より30秒以上あとの時刻を指定してください。")
    if when > now + timedelta(days=MAX_AHEAD_DAYS):
        raise HTTPException(status_code=400, detail="1年以内の時刻を指定してください。")
    return when


def check_access(session: Session, user: User, scope: str, target_id: int) -> None:
    """その場所に投稿する権限があるか確かめる。"""
    if scope == "openchat":
        oc = session.get(OpenChat, target_id)
        if not oc:
            raise HTTPException(status_code=404, detail="グループが見つかりません。")
        member = session.exec(
            select(OpenChatMember).where(
                OpenChatMember.openchat_id == target_id,
                OpenChatMember.user_id == user.id,
            )
        ).first()
        if not member:
            raise HTTPException(status_code=403, detail="このグループに参加していません。")
    elif scope == "dm":
        conv = session.get(DMConversation, target_id)
        if not conv or user.id not in (conv.user_a, conv.user_b):
            raise HTTPException(status_code=404, detail="この会話は見つかりません。")
    else:
        raise HTTPException(status_code=400, detail="送信先の種類が不正です。")


def _schedule_payload(row: ScheduledMessage) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "target_id": row.target_id,
        "content": row.content,
        "send_at": row.send_at.isoformat() + "Z",
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at.isoformat() + "Z",
        "sent_at": (row.sent_at.isoformat() + "Z") if row.sent_at else None,
    }


def _reminder_payload(row: Reminder) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "target_id": row.target_id,
        "text": row.text,
        "remind_at": row.remind_at.isoformat() + "Z",
        "post_in_chat": row.post_in_chat,
        "status": row.status,
        "created_at": row.created_at.isoformat() + "Z",
        "fired_at": (row.fired_at.isoformat() + "Z") if row.fired_at else None,
    }


@router.post("/messages")
def create_scheduled(
    payload: ScheduleCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    if len(content) > MAX_CONTENT:
        raise HTTPException(status_code=400, detail=f"メッセージは{MAX_CONTENT}文字以内にしてください。")
    check_access(session, user, payload.scope, payload.target_id)
    when = validate_when(parse_time(payload.send_at))

    pending = session.exec(
        select(ScheduledMessage).where(
            ScheduledMessage.author_id == user.id,
            ScheduledMessage.status == "pending",
        )
    ).all()
    if len(pending) >= MAX_PENDING:
        raise HTTPException(status_code=429, detail=f"予約できるのは{MAX_PENDING}件までです。")

    row = ScheduledMessage(
        author_id=user.id,
        scope=payload.scope,
        target_id=payload.target_id,
        content=content,
        send_at=when,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _schedule_payload(row)


@router.get("/messages")
def list_scheduled(
    scope: str = "",
    target_id: int = 0,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    query = select(ScheduledMessage).where(ScheduledMessage.author_id == user.id)
    if scope:
        query = query.where(ScheduledMessage.scope == scope)
    if target_id:
        query = query.where(ScheduledMessage.target_id == target_id)
    rows = session.exec(query.order_by(ScheduledMessage.send_at)).all()
    return {"scheduled": [_schedule_payload(r) for r in rows]}


@router.post("/messages/{schedule_id}/cancel")
def cancel_scheduled(
    schedule_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    row = session.get(ScheduledMessage, schedule_id)
    if not row or row.author_id != user.id:
        raise HTTPException(status_code=404, detail="その予約は見つかりません。")
    if row.status != "pending":
        raise HTTPException(status_code=400, detail="この予約はすでに処理されています。")
    row.status = "canceled"
    session.add(row)
    session.commit()
    return {"canceled": True}


@router.post("/reminders")
def create_reminder(
    payload: ReminderCreate,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="リマインドの内容を入力してください。")
    if len(text) > MAX_CONTENT:
        raise HTTPException(status_code=400, detail=f"内容は{MAX_CONTENT}文字以内にしてください。")
    post_in_chat = payload.post_in_chat
    if payload.target_id:
        check_access(session, user, payload.scope, payload.target_id)
    else:
        post_in_chat = False
    when = validate_when(parse_time(payload.remind_at))

    pending = session.exec(
        select(Reminder).where(Reminder.user_id == user.id, Reminder.status == "pending")
    ).all()
    if len(pending) >= MAX_PENDING:
        raise HTTPException(status_code=429, detail=f"設定できるリマインドは{MAX_PENDING}件までです。")

    row = Reminder(
        user_id=user.id,
        scope=payload.scope,
        target_id=payload.target_id,
        text=text,
        remind_at=when,
        post_in_chat=post_in_chat,
        source_message_id=payload.source_message_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _reminder_payload(row)


@router.get("/reminders")
def list_reminders(user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = session.exec(
        select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.remind_at)
    ).all()
    return {"reminders": [_reminder_payload(r) for r in rows]}


@router.post("/reminders/{reminder_id}/cancel")
def cancel_reminder(
    reminder_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    row = session.get(Reminder, reminder_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="そのリマインドは見つかりません。")
    if row.status != "pending":
        raise HTTPException(status_code=400, detail="このリマインドはすでに実行されています。")
    row.status = "canceled"
    session.add(row)
    session.commit()
    return {"canceled": True}


async def _deliver_scheduled(session: Session, row: ScheduledMessage) -> None:
    """予約されたメッセージを実際に投稿する。"""
    author = session.get(User, row.author_id)
    if not author or not author.is_active:
        raise RuntimeError("送信者のアカウントが利用できません。")

    if row.scope == "openchat":
        from app.routes_openchat import _member_ids, _message_public
        member = session.exec(
            select(OpenChatMember).where(
                OpenChatMember.openchat_id == row.target_id,
                OpenChatMember.user_id == author.id,
            )
        ).first()
        if not member:
            raise RuntimeError("グループから退会しているため送信できません。")
        msg = OpenChatMessage(
            openchat_id=row.target_id,
            author_id=author.id,
            content=row.content,
            attachments="[]",
        )
        session.add(msg)
        session.commit()
        session.refresh(msg)
        row.message_id = msg.id
        data = _message_public(session, msg, author=author)
        await manager.send_to_users(_member_ids(session, row.target_id), {"type": "message.openchat", "data": data})
    else:
        from app.routes_dm import _dm_public
        conv = session.get(DMConversation, row.target_id)
        if not conv or author.id not in (conv.user_a, conv.user_b):
            raise RuntimeError("この会話には送信できません。")
        other_id = conv.user_b if conv.user_a == author.id else conv.user_a
        msg = DMMessage(
            conversation_id=conv.id,
            author_id=author.id,
            content=row.content,
            attachments="[]",
        )
        session.add(msg)
        session.commit()
        session.refresh(msg)
        row.message_id = msg.id
        data = _dm_public(session, msg, author=author)
        await manager.send_to_users([author.id, other_id], {"type": "message.dm", "data": data})
        from app.routes_notifications import create_notification
        await create_notification(
            session, other_id, "dm", f"{author.username} さんからのメッセージ",
            (row.content or "")[:60], actor_id=author.id, target_type="dm", target_id=conv.id,
        )


async def _deliver_reminder(session: Session, row: Reminder) -> None:
    """リマインドを通知し、必要ならトークにも投稿する。"""
    from app.routes_notifications import create_notification

    user = session.get(User, row.user_id)
    if not user or not user.is_active:
        raise RuntimeError("対象のアカウントが利用できません。")

    await create_notification(
        session, user.id, "reminder", "リマインド", (row.text or "")[:60],
        actor_id=None,
        target_type=row.scope if row.target_id else None,
        target_id=row.target_id,
    )

    if row.post_in_chat and row.target_id and row.scope == "openchat":
        from app.routes_openchat import _member_ids, _message_public
        member = session.exec(
            select(OpenChatMember).where(
                OpenChatMember.openchat_id == row.target_id,
                OpenChatMember.user_id == user.id,
            )
        ).first()
        if member:
            msg = OpenChatMessage(
                openchat_id=row.target_id,
                author_id=user.id,
                content=f"[リマインド] {row.text}",
                attachments="[]",
            )
            session.add(msg)
            session.commit()
            session.refresh(msg)
            data = _message_public(session, msg, author=user)
            await manager.send_to_users(_member_ids(session, row.target_id), {"type": "message.openchat", "data": data})


async def run_due(now: datetime | None = None) -> tuple:
    """期限が来た予約とリマインドを実行する。処理した件数を返す。"""
    cutoff = now or datetime.utcnow()
    sent = 0
    fired = 0
    with Session(engine) as session:
        rows = session.exec(
            select(ScheduledMessage).where(
                ScheduledMessage.status == "pending",
                ScheduledMessage.send_at <= cutoff,
            ).order_by(ScheduledMessage.send_at).limit(50)
        ).all()
        for row in rows:
            row.status = "sending"
            session.add(row)
            session.commit()
            try:
                await _deliver_scheduled(session, row)
                row.status = "sent"
                row.sent_at = datetime.utcnow()
                sent += 1
            except Exception as exc:
                row.status = "failed"
                row.error = str(exc)[:300]
                print(f"[schedule] 予約送信に失敗しました #{row.id}: {exc}")
            session.add(row)
            session.commit()

        reminders = session.exec(
            select(Reminder).where(
                Reminder.status == "pending",
                Reminder.remind_at <= cutoff,
            ).order_by(Reminder.remind_at).limit(50)
        ).all()
        for row in reminders:
            row.status = "firing"
            session.add(row)
            session.commit()
            try:
                await _deliver_reminder(session, row)
                row.status = "done"
                row.fired_at = datetime.utcnow()
                fired += 1
            except Exception as exc:
                row.status = "failed"
                print(f"[schedule] リマインドに失敗しました #{row.id}: {exc}")
            session.add(row)
            session.commit()
    return sent, fired


async def worker() -> None:
    """20秒ごとに期限を確認し続ける。"""
    print(f"[schedule] 予約送信ワーカーを開始しました ({TICK_SECONDS}秒間隔)")
    while True:
        try:
            await run_due()
        except Exception as exc:
            print(f"[schedule] ワーカーでエラーが発生しました: {exc}")
        await asyncio.sleep(TICK_SECONDS)
