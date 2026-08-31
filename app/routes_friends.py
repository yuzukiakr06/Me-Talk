"""Me Talk フレンド機能ルーター。

申請→承認/拒否のフレンド関係。DMは誰にでも送れるが、通話はフレンドのみ。
申請・承認・拒否・削除・一覧・状態確認・共通フレンドを提供する。

フレンドとフォローは役割が異なる。
フレンドは相互の同意が必要な「親しい関係」、
フォローはグローバルチャットで投稿を受け取るだけの一方的な購読。
フレンドになった時点で、互いの投稿は見たいはずなので相互フォローも張る。
ただしフォローの解除は自由で、外してもフレンド関係は続く。

Dev yuzuki_akrdev.ofc
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select, or_, and_

from app.db import get_session
from app.models import Follow, Friendship, OpenChatMember, User
from app.realtime import manager
from app.routes_auth import current_user, public_user
from app.routes_notifications import create_notification_sync

router = APIRouter(prefix="/api/friends", tags=["friends"])


class FriendRequest(BaseModel):
    user_id: int


def _ensure_mutual_follow(session: Session, a: int, b: int) -> None:
    """フレンドになった2人の間に相互フォローを張る。すでにあれば何もしない。"""
    for follower, followee in ((a, b), (b, a)):
        existing = session.exec(
            select(Follow).where(Follow.follower_id == follower, Follow.followee_id == followee)
        ).first()
        if not existing:
            session.add(Follow(follower_id=follower, followee_id=followee))
    session.commit()


def _friendship(session: Session, a: int, b: int) -> Friendship | None:
    return session.exec(
        select(Friendship).where(
            or_(
                and_(Friendship.requester_id == a, Friendship.addressee_id == b),
                and_(Friendship.requester_id == b, Friendship.addressee_id == a),
            )
        )
    ).first()


def are_friends(session: Session, a: int, b: int) -> bool:
    f = _friendship(session, a, b)
    return f is not None and f.status == "accepted"


def _friend_ids(session: Session, user_id: int) -> set[int]:
    rows = session.exec(
        select(Friendship).where(
            Friendship.status == "accepted",
            or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id),
        )
    ).all()
    ids = set()
    for f in rows:
        ids.add(f.addressee_id if f.requester_id == user_id else f.requester_id)
    return ids


def friendship_state(session: Session, me: int, other: int) -> str:
    if me == other:
        return "self"
    f = _friendship(session, me, other)
    if not f:
        return "none"
    if f.status == "accepted":
        return "friends"
    if f.status == "pending":
        return "outgoing" if f.requester_id == me else "incoming"
    return "none"


@router.get("/status/{user_id}")
def get_status(user_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    return {"state": friendship_state(session, user.id, user_id)}


@router.post("/request")
def send_request(payload: FriendRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if payload.user_id == user.id:
        raise HTTPException(status_code=400, detail="自分自身には申請できません。")
    other = session.get(User, payload.user_id)
    if not other or not other.is_active:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    from app import social
    if social.is_blocked(session, user.id, other.id):
        raise HTTPException(status_code=403, detail="このユーザーには申請できません。")
    existing = _friendship(session, user.id, other.id)
    if existing:
        if existing.status == "accepted":
            return {"state": "friends"}
        if existing.status == "pending":
            if existing.requester_id == user.id:
                return {"state": "outgoing"}
            existing.status = "accepted"
            existing.accepted_at = datetime.utcnow()
            session.add(existing)
            session.commit()
            _ensure_mutual_follow(session, user.id, other.id)
            return {"state": "friends"}
        existing.requester_id = user.id
        existing.addressee_id = other.id
        existing.status = "pending"
        session.add(existing)
        session.commit()
        return {"state": "outgoing"}
    session.add(Friendship(requester_id=user.id, addressee_id=other.id, status="pending"))
    session.commit()
    create_notification_sync(
        session, other.id, "friend_request", f"{user.username} さんからフレンド申請",
        "フレンド申請が届きました", actor_id=user.id, target_type="friend", target_id=user.id,
    )
    return {"state": "outgoing"}


@router.post("/accept")
def accept_request(payload: FriendRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    f = _friendship(session, user.id, payload.user_id)
    if not f or f.status != "pending" or f.addressee_id != user.id:
        raise HTTPException(status_code=404, detail="承認できる申請がありません。")
    f.status = "accepted"
    f.accepted_at = datetime.utcnow()
    session.add(f)
    session.commit()
    _ensure_mutual_follow(session, user.id, payload.user_id)
    create_notification_sync(
        session, f.requester_id, "friend_accept", f"{user.username} さんがフレンド申請を承認しました",
        "フレンドになりました", actor_id=user.id, target_type="friend", target_id=user.id,
    )
    return {"state": "friends"}


@router.post("/decline")
def decline_request(payload: FriendRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    f = _friendship(session, user.id, payload.user_id)
    if not f or f.status != "pending":
        raise HTTPException(status_code=404, detail="拒否できる申請がありません。")
    session.delete(f)
    session.commit()
    return {"state": "none"}


@router.post("/remove")
def remove_friend(payload: FriendRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    f = _friendship(session, user.id, payload.user_id)
    if f:
        session.delete(f)
        session.commit()
    return {"state": "none"}


@router.get("")
def list_friends(user: User = Depends(current_user), session: Session = Depends(get_session)):
    ids = _friend_ids(session, user.id)
    friends = []
    for uid in ids:
        u = session.get(User, uid)
        if u:
            entry = public_user(u)
            entry["online"] = manager.is_online(u.id)
            friends.append(entry)
    friends.sort(key=lambda x: (not x["online"], x["username"]))
    return {"friends": friends}


@router.get("/requests")
def list_requests(user: User = Depends(current_user), session: Session = Depends(get_session)):
    incoming = session.exec(
        select(Friendship).where(Friendship.addressee_id == user.id, Friendship.status == "pending")
    ).all()
    outgoing = session.exec(
        select(Friendship).where(Friendship.requester_id == user.id, Friendship.status == "pending")
    ).all()

    def enrich(uid):
        u = session.get(User, uid)
        if not u:
            return None
        e = public_user(u)
        e["online"] = manager.is_online(u.id)
        return e

    return {
        "incoming": [x for x in (enrich(f.requester_id) for f in incoming) if x],
        "outgoing": [x for x in (enrich(f.addressee_id) for f in outgoing) if x],
    }


@router.get("/mutual/{user_id}")
def mutual(user_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not are_friends(session, user.id, user_id):
        raise HTTPException(status_code=403, detail="共通の詳細はフレンドのみ表示できます。")
    my_friends = _friend_ids(session, user.id)
    their_friends = _friend_ids(session, user_id)
    common_friend_ids = (my_friends & their_friends) - {user.id, user_id}
    common_friends = []
    for uid in common_friend_ids:
        u = session.get(User, uid)
        if u:
            common_friends.append(public_user(u))
    my_groups = {m.openchat_id for m in session.exec(select(OpenChatMember).where(OpenChatMember.user_id == user.id)).all()}
    their_groups = {m.openchat_id for m in session.exec(select(OpenChatMember).where(OpenChatMember.user_id == user_id)).all()}
    from app.models import OpenChat
    common_groups = []
    for gid in (my_groups & their_groups):
        g = session.get(OpenChat, gid)
        if g:
            common_groups.append({"id": g.id, "name": g.name, "icon_url": g.icon_url})
    return {"friends": common_friends, "groups": common_groups}
