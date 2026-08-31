"""Me Talk オープンチャット(グループ)ルーター。

チャンネルの概念は持たず、1グループ=1つのチャット空間。
作成・一覧(自分の)・discover(公開一覧)・参加・退出・メンバー・メッセージ取得/投稿を提供する。
メッセージ投稿時は WebSocket でグループのオンラインメンバーへ配信する。

Dev yuzuki_akrdev.ofc
"""

import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.models import MessageHidden, OpenChat, OpenChatEvent, OpenChatInvite, OpenChatMember, OpenChatMessage, OpenChatMute, OpenChatNote, OpenChatPin, User
from app.realtime import manager
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/openchats", tags=["openchats"])

MAX_MESSAGE_LEN = 4000
SEARCH_SCAN_LIMIT = 3000
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_TOTAL = 50 * 1024 * 1024


class CreateOpenChat(BaseModel):
    name: str
    description: str = ""
    visibility: str = "public"


class PostMessage(BaseModel):
    content: str = ""
    reply_to: int | None = None
    attachments: list[dict] = []
    anonymous: bool = False


def _openchat_public(oc: OpenChat, member_count: int, joined: bool) -> dict:
    return {
        "id": oc.id,
        "name": oc.name,
        "description": oc.description,
        "icon_url": oc.icon_url,
        "cover_url": oc.cover_url,
        "room_bg_url": oc.room_bg_url,
        "owner_id": oc.owner_id,
        "visibility": oc.visibility,
        "allow_anonymous": bool(oc.allow_anonymous),
        "member_count": member_count,
        "joined": joined,
        "created_at": oc.created_at.isoformat(),
    }


def _member_count(session: Session, openchat_id: int) -> int:
    return len(session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all())


def _membership(session: Session, openchat_id: int, user_id: int) -> OpenChatMember | None:
    return session.exec(
        select(OpenChatMember).where(
            OpenChatMember.openchat_id == openchat_id,
            OpenChatMember.user_id == user_id,
        )
    ).first()


def _member_ids(session: Session, openchat_id: int) -> list[int]:
    return [m.user_id for m in session.exec(
        select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)
    ).all()]


ANON_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _anon_label(session: Session, openchat_id: int, user_id: int) -> str:
    """グループ内で安定した匿名の呼び名を割り当てる。

    同じ人が何度投稿しても同じ呼び名になるので会話が追える。
    一方で、そのグループの中では発言がひとつながりに見える点は
    利用者に明示しておく必要がある。
    """
    member = _membership(session, openchat_id, user_id)
    if not member:
        return "匿名"
    if member.anon_label:
        return member.anon_label
    used = {
        m.anon_label for m in session.exec(
            select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)
        ).all() if m.anon_label
    }
    index = 0
    while True:
        first = ANON_ALPHABET[index % 26]
        suffix = "" if index < 26 else str(index // 26 + 1)
        label = f"匿名{first}{suffix}"
        if label not in used:
            break
        index += 1
    member.anon_label = label
    session.add(member)
    session.commit()
    return label


def _message_public(session: Session, msg: OpenChatMessage, author: User | None = None) -> dict:
    """メッセージを公開用に整える。

    匿名投稿は誰から見ても投稿者を伏せる。
    投稿者本人の記録はデータベースに残るため、
    違反対応が必要な場合に運営だけが辿れる。
    """
    if author is None and not msg.anonymous:
        author = session.get(User, msg.author_id)
    try:
        attachments = json.loads(msg.attachments) if msg.attachments else []
    except (json.JSONDecodeError, TypeError):
        attachments = []
    data = {
        "id": msg.id,
        "openchat_id": msg.openchat_id,
        "content": msg.content,
        "reply_to": msg.reply_to,
        "attachments": attachments,
        "created_at": msg.created_at.isoformat(),
        "edited_at": msg.edited_at.isoformat() if msg.edited_at else None,
        "anonymous": bool(msg.anonymous),
    }
    if msg.anonymous:
        data["author"] = None
        data["anon_label"] = msg.anon_label or "匿名"
    else:
        data["author"] = public_user(author) if author else None
    return data


@router.post("")
def create_openchat(payload: CreateOpenChat, user: User = Depends(current_user), session: Session = Depends(get_session)):
    name = payload.name.strip()
    if not (1 <= len(name) <= 60):
        raise HTTPException(status_code=400, detail="グループ名は1〜60文字にしてください。")
    if payload.visibility not in {"public", "invite"}:
        raise HTTPException(status_code=400, detail="公開設定が不正です。")
    oc = OpenChat(
        name=name,
        description=payload.description.strip()[:500],
        owner_id=user.id,
        visibility=payload.visibility,
    )
    session.add(oc)
    session.commit()
    session.refresh(oc)
    session.add(OpenChatMember(openchat_id=oc.id, user_id=user.id, role="owner"))
    session.commit()
    return _openchat_public(oc, 1, True)


@router.get("")
def my_openchats(user: User = Depends(current_user), session: Session = Depends(get_session)):
    memberships = session.exec(select(OpenChatMember).where(OpenChatMember.user_id == user.id)).all()
    result = []
    for m in memberships:
        oc = session.get(OpenChat, m.openchat_id)
        if oc:
            result.append(_openchat_public(oc, _member_count(session, oc.id), True))
    return {"openchats": result}


@router.get("/discover")
def discover(session: Session = Depends(get_session), user: User = Depends(current_user)):
    chats = session.exec(
        select(OpenChat).where(OpenChat.visibility == "public").order_by(OpenChat.created_at.desc()).limit(50)
    ).all()
    result = []
    for oc in chats:
        joined = _membership(session, oc.id, user.id) is not None
        result.append(_openchat_public(oc, _member_count(session, oc.id), joined))
    return {"openchats": result}


@router.get("/{openchat_id}")
def get_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    joined = _membership(session, oc.id, user.id) is not None
    if oc.visibility != "public" and not joined:
        raise HTTPException(status_code=403, detail="このグループに参加していません。")
    return _openchat_public(oc, _member_count(session, oc.id), joined)


@router.post("/{openchat_id}/join")
def join_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    if oc.visibility != "public":
        raise HTTPException(status_code=403, detail="このグループは招待制です。")
    if _membership(session, oc.id, user.id):
        return {"joined": True}
    session.add(OpenChatMember(openchat_id=oc.id, user_id=user.id, role="member"))
    session.commit()
    return {"joined": True}


@router.post("/{openchat_id}/leave")
def leave_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    membership = _membership(session, oc.id, user.id)
    if not membership:
        return {"left": True}
    if oc.owner_id == user.id:
        raise HTTPException(status_code=400, detail="オーナーは退出できません。グループを削除してください。")
    session.delete(membership)
    session.commit()
    return {"left": True}


@router.get("/{openchat_id}/members")
def list_members(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    if not _membership(session, oc.id, user.id) and oc.visibility != "public":
        raise HTTPException(status_code=403, detail="このグループに参加していません。")
    members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
    result = []
    for m in members:
        u = session.get(User, m.user_id)
        if u:
            entry = public_user(u)
            entry["role"] = m.role
            entry["online"] = manager.is_online(u.id)
            result.append(entry)
    return {"members": result}


@router.get("/{openchat_id}/messages")
def get_messages(
    openchat_id: int,
    before: int | None = Query(default=None),
    limit: int = Query(default=50, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    if not _membership(session, oc.id, user.id) and oc.visibility != "public":
        raise HTTPException(status_code=403, detail="このグループに参加していません。")
    query = select(OpenChatMessage).where(
        OpenChatMessage.openchat_id == openchat_id,
        OpenChatMessage.deleted == False,  # noqa: E712
    )
    if before:
        query = query.where(OpenChatMessage.id < before)
    query = query.order_by(OpenChatMessage.id.desc()).limit(limit)
    messages = session.exec(query).all()
    hidden = {
        h.message_id for h in session.exec(
            select(MessageHidden).where(MessageHidden.user_id == user.id, MessageHidden.scope == "openchat")
        ).all()
    }
    messages = [m for m in messages if m.id not in hidden]
    messages = _visible_messages(session, user.id, messages)
    messages.reverse()
    from app.routes_reactions import reactions_for
    react_map = reactions_for(session, "openchat", [m.id for m in messages], user.id)
    result = []
    for m in messages:
        d = _message_public(session, m)
        d["reactions"] = react_map.get(m.id, [])
        result.append(d)
    return {"messages": result}


@router.post("/{openchat_id}/messages")
async def post_message(
    openchat_id: int,
    payload: PostMessage,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    membership = _membership(session, oc.id, user.id)
    if not membership:
        if oc.visibility == "public":
            session.add(OpenChatMember(openchat_id=oc.id, user_id=user.id, role="member"))
            session.commit()
        else:
            raise HTTPException(status_code=403, detail="このグループに参加していません。")
    content = payload.content.strip()
    attachments = payload.attachments or []
    if not content and not attachments:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    if len(content) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"メッセージは{MAX_MESSAGE_LEN}文字以内にしてください。")
    if len(attachments) > MAX_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"添付は{MAX_ATTACHMENTS}件までです。")

    clean_attachments = []
    total_size = 0
    for a in attachments:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url", ""))[:500]
        if not url:
            continue
        size = int(a.get("size", 0)) if str(a.get("size", "")).isdigit() else 0
        total_size += size
        clean_attachments.append({
            "kind": str(a.get("kind", "file"))[:16],
            "url": url,
            "name": str(a.get("name", ""))[:120],
            "content_type": str(a.get("content_type", ""))[:80],
            "size": size,
        })

    if total_size > MAX_ATTACHMENT_TOTAL:
        raise HTTPException(status_code=400, detail="添付の合計サイズが上限を超えています(1メッセージあたり50MBまで)。")

    anonymous = bool(payload.anonymous)
    anon_label = ""
    if anonymous:
        if not oc.allow_anonymous:
            raise HTTPException(status_code=400, detail="このグループでは匿名投稿が許可されていません。")
        if request.headers.get("X-Me-Talk-Client", "").lower() != "app":
            raise HTTPException(status_code=403, detail="匿名投稿はアプリ版でのみ利用できます。")
        anon_label = _anon_label(session, openchat_id, user.id)

    msg = OpenChatMessage(
        openchat_id=openchat_id,
        author_id=user.id,
        content=content,
        anonymous=anonymous,
        anon_label=anon_label,
        reply_to=payload.reply_to,
        attachments=json.dumps(clean_attachments, ensure_ascii=False),
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)

    data = _message_public(session, msg, author=None if anonymous else user)
    member_ids = _member_ids(session, openchat_id)
    await manager.send_to_users(member_ids, {"type": "message.openchat", "data": data})
    if content and "@" in content:
        import re as _re
        from app.routes_notifications import create_notification
        oc = session.get(OpenChat, openchat_id)
        mentioned = set(_re.findall(r"@([A-Za-z0-9_]{3,20})", content))
        if mentioned:
            members = session.exec(
                select(User).join(OpenChatMember, OpenChatMember.user_id == User.id)
                .where(OpenChatMember.openchat_id == openchat_id)
            ).all()
            actor_name = anon_label if anonymous else user.username
            for mem in members:
                if mem.id != user.id and mem.username in mentioned:
                    await create_notification(
                        session, mem.id, "mention",
                        f"{oc.name if oc else 'グループ'} で {actor_name} さんがメンション",
                        content[:60], actor_id=user.id, target_type="openchat", target_id=openchat_id,
                    )
    return data


class CreateInvite(BaseModel):
    max_uses: int | None = None
    expires_hours: int | None = None


def _visible_messages(session: Session, viewer_id: int, messages: list) -> list:
    """ブロックしている相手のメッセージを取り除く。

    匿名投稿もブロック対象なら隠す。表示上は匿名でも、
    サーバー側では投稿者が分かるため判定できる。
    """
    from app import social
    hidden = social.blocked_ids(session, viewer_id)
    if not hidden:
        return messages
    return [m for m in messages if m.author_id not in hidden]


def _require_membership(session: Session, openchat_id: int, user_id: int) -> tuple[OpenChat, OpenChatMember]:
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    membership = _membership(session, openchat_id, user_id)
    if not membership:
        raise HTTPException(status_code=403, detail="このグループに参加していません。")
    return oc, membership


@router.post("/{openchat_id}/invites")
def create_invite(openchat_id: int, payload: CreateInvite, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    expires_at = None
    if payload.expires_hours and payload.expires_hours > 0:
        expires_at = datetime.utcnow() + timedelta(hours=payload.expires_hours)
    code = secrets.token_urlsafe(8)
    invite = OpenChatInvite(
        openchat_id=openchat_id,
        code=code,
        created_by=user.id,
        max_uses=payload.max_uses if payload.max_uses and payload.max_uses > 0 else None,
        expires_at=expires_at,
    )
    session.add(invite)
    session.commit()
    return {
        "code": code,
        "url": f"{config.BASE_URL}/invite/{code}",
        "max_uses": invite.max_uses,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
    }


@router.get("/{openchat_id}/invites")
def list_invites(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    invites = session.exec(select(OpenChatInvite).where(OpenChatInvite.openchat_id == openchat_id)).all()
    now = datetime.utcnow()
    result = []
    for inv in invites:
        active = True
        if inv.expires_at and inv.expires_at < now:
            active = False
        if inv.max_uses is not None and inv.uses >= inv.max_uses:
            active = False
        result.append({
            "code": inv.code,
            "url": f"{config.BASE_URL}/invite/{inv.code}",
            "uses": inv.uses,
            "max_uses": inv.max_uses,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "active": active,
        })
    return {"invites": result}


@router.post("/invites/{code}/accept")
def accept_invite(code: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    invite = session.exec(select(OpenChatInvite).where(OpenChatInvite.code == code)).first()
    if not invite:
        raise HTTPException(status_code=404, detail="招待が見つかりません。")
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="この招待は期限切れです。")
    if invite.max_uses is not None and invite.uses >= invite.max_uses:
        raise HTTPException(status_code=410, detail="この招待は使用回数の上限に達しました。")
    oc = session.get(OpenChat, invite.openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    if not _membership(session, oc.id, user.id):
        session.add(OpenChatMember(openchat_id=oc.id, user_id=user.id, role="member"))
        invite.uses += 1
        session.add(invite)
        session.commit()
    return {"joined": True, "openchat": _openchat_public(oc, _member_count(session, oc.id), True)}


@router.get("/{openchat_id}/messages/search")
def search_messages(
    openchat_id: int,
    q: str = Query(min_length=1),
    limit: int = Query(default=30, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="検索キーワードを入力してください。")
    query = select(OpenChatMessage).where(
        OpenChatMessage.openchat_id == openchat_id,
        OpenChatMessage.deleted == False,  # noqa: E712
    ).order_by(OpenChatMessage.id.desc()).limit(SEARCH_SCAN_LIMIT)
    lowered = keyword.lower()
    found = _visible_messages(session, user.id, session.exec(query).all())
    messages = [m for m in found if lowered in (m.content or "").lower()][:limit]
    return {"query": keyword, "messages": [_message_public(session, m) for m in messages]}


@router.get("/{openchat_id}/members/search")
def search_members(
    openchat_id: int,
    q: str = Query(min_length=1),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    keyword = q.strip().lower()
    members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
    result = []
    for m in members:
        u = session.get(User, m.user_id)
        if u and (keyword in u.username.lower() or keyword in (u.display_name or "").lower()):
            entry = public_user(u)
            entry["role"] = m.role
            entry["online"] = manager.is_online(u.id)
            result.append(entry)
    return {"query": q.strip(), "members": result}


class UpdateAnonymous(BaseModel):
    allow_anonymous: bool


@router.patch("/{openchat_id}/anonymous")
def update_anonymous(openchat_id: int, payload: UpdateAnonymous, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """グループでの匿名投稿の可否を切り替える。オーナーのみ。"""
    oc = _require_owner(session, openchat_id, user.id)
    oc.allow_anonymous = bool(payload.allow_anonymous)
    session.add(oc)
    session.commit()
    return {"allow_anonymous": oc.allow_anonymous}


class UpdateProfile(BaseModel):
    name: str | None = None
    description: str | None = None
    icon_url: str | None = None
    cover_url: str | None = None
    room_bg_url: str | None = None


def _require_owner(session: Session, openchat_id: int, user_id: int) -> OpenChat:
    oc = session.get(OpenChat, openchat_id)
    if not oc:
        raise HTTPException(status_code=404, detail="グループが見つかりません。")
    if oc.owner_id != user_id:
        raise HTTPException(status_code=403, detail="この操作はオーナーのみ可能です。")
    return oc


@router.patch("/{openchat_id}/profile")
def update_profile(openchat_id: int, payload: UpdateProfile, user: User = Depends(current_user), session: Session = Depends(get_session)):
    oc = _require_owner(session, openchat_id, user.id)
    if payload.name is not None:
        name = payload.name.strip()
        if not (1 <= len(name) <= 60):
            raise HTTPException(status_code=400, detail="グループ名は1〜60文字にしてください。")
        oc.name = name
    if payload.description is not None:
        oc.description = payload.description.strip()[:500]
    if payload.icon_url is not None:
        oc.icon_url = payload.icon_url or None
    if payload.cover_url is not None:
        oc.cover_url = payload.cover_url or None
    if payload.room_bg_url is not None:
        oc.room_bg_url = payload.room_bg_url or None
    session.add(oc)
    session.commit()
    session.refresh(oc)
    return _openchat_public(oc, _member_count(session, oc.id), True)


class CreateEvent(BaseModel):
    title: str
    body: str = ""
    starts_at: datetime
    ends_at: datetime | None = None
    all_day: bool = False


def _event_public(session: Session, ev: OpenChatEvent) -> dict:
    author = session.get(User, ev.author_id)
    return {
        "id": ev.id,
        "title": ev.title,
        "body": ev.body,
        "starts_at": ev.starts_at.isoformat(),
        "ends_at": ev.ends_at.isoformat() if ev.ends_at else None,
        "all_day": ev.all_day,
        "author": public_user(author) if author else None,
        "created_at": ev.created_at.isoformat(),
    }


@router.get("/{openchat_id}/events")
def list_events(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    events = session.exec(
        select(OpenChatEvent).where(OpenChatEvent.openchat_id == openchat_id).order_by(OpenChatEvent.starts_at)
    ).all()
    return {"events": [_event_public(session, e) for e in events]}


@router.post("/{openchat_id}/events")
def create_event(openchat_id: int, payload: CreateEvent, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    title = payload.title.strip()
    if not (1 <= len(title) <= 100):
        raise HTTPException(status_code=400, detail="予定のタイトルは1〜100文字にしてください。")
    ev = OpenChatEvent(
        openchat_id=openchat_id,
        author_id=user.id,
        title=title,
        body=payload.body.strip()[:1000],
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        all_day=payload.all_day,
    )
    session.add(ev)
    session.commit()
    session.refresh(ev)
    return _event_public(session, ev)


@router.delete("/{openchat_id}/events/{event_id}")
def delete_event(openchat_id: int, event_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    ev = session.get(OpenChatEvent, event_id)
    if not ev or ev.openchat_id != openchat_id:
        raise HTTPException(status_code=404, detail="予定が見つかりません。")
    oc = session.get(OpenChat, openchat_id)
    if ev.author_id != user.id and oc.owner_id != user.id:
        raise HTTPException(status_code=403, detail="作成者またはオーナーのみ削除できます。")
    session.delete(ev)
    session.commit()
    return {"deleted": True}


class CreateNote(BaseModel):
    title: str
    body: str = ""
    pinned: bool = False


def _note_public(session: Session, note: OpenChatNote) -> dict:
    author = session.get(User, note.author_id)
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "pinned": note.pinned,
        "author": public_user(author) if author else None,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
    }


@router.get("/{openchat_id}/notes")
def list_notes(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    notes = session.exec(
        select(OpenChatNote).where(OpenChatNote.openchat_id == openchat_id).order_by(OpenChatNote.pinned.desc(), OpenChatNote.id.desc())
    ).all()
    return {"notes": [_note_public(session, n) for n in notes]}


@router.post("/{openchat_id}/notes")
def create_note(openchat_id: int, payload: CreateNote, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    title = payload.title.strip()
    if not (1 <= len(title) <= 100):
        raise HTTPException(status_code=400, detail="ノートのタイトルは1〜100文字にしてください。")
    note = OpenChatNote(
        openchat_id=openchat_id,
        author_id=user.id,
        title=title,
        body=payload.body.strip()[:5000],
        pinned=payload.pinned,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return _note_public(session, note)


@router.delete("/{openchat_id}/notes/{note_id}")
def delete_note(openchat_id: int, note_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    note = session.get(OpenChatNote, note_id)
    if not note or note.openchat_id != openchat_id:
        raise HTTPException(status_code=404, detail="ノートが見つかりません。")
    oc = session.get(OpenChat, openchat_id)
    if note.author_id != user.id and oc.owner_id != user.id:
        raise HTTPException(status_code=403, detail="作成者またはオーナーのみ削除できます。")
    session.delete(note)
    session.commit()
    return {"deleted": True}


def _collect_attachments(session: Session, openchat_id: int, kinds: set[str], limit: int = 100) -> list[dict]:
    messages = session.exec(
        select(OpenChatMessage).where(
            OpenChatMessage.openchat_id == openchat_id,
            OpenChatMessage.deleted == False,  # noqa: E712
        ).order_by(OpenChatMessage.id.desc())
    ).all()
    items = []
    for m in messages:
        if not m.attachments:
            continue
        try:
            atts = json.loads(m.attachments)
        except (json.JSONDecodeError, TypeError):
            continue
        for a in atts:
            if a.get("kind") in kinds:
                items.append({
                    "message_id": m.id,
                    "author_id": m.author_id,
                    "created_at": m.created_at.isoformat(),
                    **a,
                })
                if len(items) >= limit:
                    return items
    return items


URL_RE = None


@router.get("/{openchat_id}/media")
def list_media(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    return {"media": _collect_attachments(session, openchat_id, {"image", "video"})}


@router.get("/{openchat_id}/files")
def list_files(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    return {"files": _collect_attachments(session, openchat_id, {"file"})}


@router.get("/{openchat_id}/links")
def list_links(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    import re
    url_pattern = re.compile(r"https?://[^\s<>\"]+")
    messages = session.exec(
        select(OpenChatMessage).where(
            OpenChatMessage.openchat_id == openchat_id,
            OpenChatMessage.deleted == False,  # noqa: E712
        ).order_by(OpenChatMessage.id.desc())
    ).all()
    links = []
    for m in messages:
        for url in url_pattern.findall(m.content or ""):
            links.append({
                "url": url,
                "message_id": m.id,
                "author_id": m.author_id,
                "created_at": m.created_at.isoformat(),
            })
            if len(links) >= 100:
                return {"links": links}
    return {"links": links}


@router.get("/{openchat_id}/mute")
def get_mute(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    m = session.exec(
        select(OpenChatMute).where(OpenChatMute.openchat_id == openchat_id, OpenChatMute.user_id == user.id)
    ).first()
    return {"muted": m is not None}


@router.post("/{openchat_id}/mute")
def set_mute(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    existing = session.exec(
        select(OpenChatMute).where(OpenChatMute.openchat_id == openchat_id, OpenChatMute.user_id == user.id)
    ).first()
    if existing:
        return {"muted": True}
    session.add(OpenChatMute(openchat_id=openchat_id, user_id=user.id))
    session.commit()
    return {"muted": True}


@router.delete("/{openchat_id}/mute")
def clear_mute(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    existing = session.exec(
        select(OpenChatMute).where(OpenChatMute.openchat_id == openchat_id, OpenChatMute.user_id == user.id)
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
    return {"muted": False}


class MessageIds(BaseModel):
    message_ids: list[int]


class SaveNote(BaseModel):
    message_ids: list[int]


UNSEND_WINDOW = timedelta(hours=24)
EDIT_WINDOW = timedelta(hours=1)
MAX_PINS = 3


class EditMessage(BaseModel):
    content: str


@router.patch("/{openchat_id}/messages/{message_id}")
async def edit_message(
    openchat_id: int,
    message_id: int,
    payload: EditMessage,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    msg = session.get(OpenChatMessage, message_id)
    if not msg or msg.openchat_id != openchat_id or msg.deleted:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="自分のメッセージのみ編集できます。")
    if datetime.utcnow() - msg.created_at > EDIT_WINDOW:
        raise HTTPException(status_code=400, detail="送信から1時間を過ぎたメッセージは編集できません。")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    if len(content) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"メッセージは{MAX_MESSAGE_LEN}文字以内にしてください。")
    msg.content = content
    msg.edited_at = datetime.utcnow()
    session.add(msg)
    members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
    session.commit()
    session.refresh(msg)
    data = _message_public(session, msg, author=user)
    await manager.send_to_users(
        [m.user_id for m in members],
        {"type": "message.edit", "data": data},
    )
    return data


@router.post("/{openchat_id}/messages/hide")
def hide_messages(
    openchat_id: int,
    payload: MessageIds,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    count = 0
    for mid in payload.message_ids[:100]:
        exists = session.exec(
            select(MessageHidden).where(
                MessageHidden.user_id == user.id,
                MessageHidden.scope == "openchat",
                MessageHidden.message_id == mid,
            )
        ).first()
        if not exists:
            session.add(MessageHidden(user_id=user.id, scope="openchat", message_id=mid))
            count += 1
    session.commit()
    return {"hidden": count}


@router.post("/{openchat_id}/messages/{message_id}/unsend")
async def unsend_message(
    openchat_id: int,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    msg = session.get(OpenChatMessage, message_id)
    if not msg or msg.openchat_id != openchat_id:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    if msg.author_id != user.id:
        raise HTTPException(status_code=403, detail="自分のメッセージのみ取り消せます。")
    if datetime.utcnow() - msg.created_at > UNSEND_WINDOW:
        raise HTTPException(status_code=400, detail="送信から24時間を過ぎたメッセージは取り消せません。")
    msg.deleted = True
    session.add(msg)
    members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
    session.commit()
    await manager.send_to_users(
        [m.user_id for m in members],
        {"type": "message.unsend", "data": {"openchat_id": openchat_id, "message_id": message_id, "scope": "openchat"}},
    )
    return {"unsent": True}


@router.post("/{openchat_id}/messages/save-note")
def save_messages_to_note(
    openchat_id: int,
    payload: SaveNote,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    saved = 0
    for mid in payload.message_ids[:50]:
        msg = session.get(OpenChatMessage, mid)
        if not msg or msg.openchat_id != openchat_id:
            continue
        author = session.get(User, msg.author_id)
        title = (msg.content or "添付")[:40]
        body = msg.content or ""
        if author:
            body = f"{author.username}: {body}"
        session.add(OpenChatNote(openchat_id=openchat_id, author_id=user.id, title=title, body=body))
        saved += 1
    session.commit()
    return {"saved": saved}


def _pin_public(session: Session, pin: OpenChatPin) -> dict:
    msg = session.get(OpenChatMessage, pin.message_id)
    author = session.get(User, msg.author_id) if msg else None
    return {
        "id": pin.id,
        "message_id": pin.message_id,
        "content": (msg.content if msg and not msg.deleted else "(削除済み)") if msg else "(削除済み)",
        "author": author.username if author else "?",
        "created_at": pin.created_at.isoformat(),
    }


@router.get("/{openchat_id}/pins")
def list_pins(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_membership(session, openchat_id, user.id)
    pins = session.exec(
        select(OpenChatPin).where(OpenChatPin.openchat_id == openchat_id).order_by(OpenChatPin.id.desc())
    ).all()
    return {"pins": [_pin_public(session, p) for p in pins]}


@router.post("/{openchat_id}/messages/{message_id}/pin")
async def pin_message(
    openchat_id: int,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    msg = session.get(OpenChatMessage, message_id)
    if not msg or msg.openchat_id != openchat_id:
        raise HTTPException(status_code=404, detail="メッセージが見つかりません。")
    existing = session.exec(
        select(OpenChatPin).where(OpenChatPin.openchat_id == openchat_id, OpenChatPin.message_id == message_id)
    ).first()
    if existing:
        return {"pinned": True}
    pins = session.exec(
        select(OpenChatPin).where(OpenChatPin.openchat_id == openchat_id).order_by(OpenChatPin.id.asc())
    ).all()
    while len(pins) >= MAX_PINS:
        oldest = pins.pop(0)
        session.delete(oldest)
    pin = OpenChatPin(openchat_id=openchat_id, message_id=message_id, pinned_by=user.id)
    session.add(pin)
    members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
    session.commit()
    await manager.send_to_users(
        [m.user_id for m in members],
        {"type": "pin.update", "data": {"openchat_id": openchat_id}},
    )
    return {"pinned": True}


@router.delete("/{openchat_id}/pins/{message_id}")
async def unpin_message(
    openchat_id: int,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_membership(session, openchat_id, user.id)
    pin = session.exec(
        select(OpenChatPin).where(OpenChatPin.openchat_id == openchat_id, OpenChatPin.message_id == message_id)
    ).first()
    if pin:
        session.delete(pin)
        members = session.exec(select(OpenChatMember).where(OpenChatMember.openchat_id == openchat_id)).all()
        session.commit()
        await manager.send_to_users(
            [m.user_id for m in members],
            {"type": "pin.update", "data": {"openchat_id": openchat_id}},
        )
    return {"unpinned": True}
