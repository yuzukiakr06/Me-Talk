"""Me Talk ブロック管理。

ブロックはオープンチャット・DM・グローバルチャットの全体に効く。
判定そのものは app/social.py に集約し、ここでは登録と解除だけを扱う。

Dev yuzuki_akrdev.ofc
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app import social
from app.db import get_session
from app.models import Block, Follow, Friendship, User
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/blocks", tags=["blocks"])


@router.get("")
def list_blocks(user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = session.exec(
        select(Block).where(Block.user_id == user.id).order_by(Block.created_at.desc())
    ).all()
    users = []
    for row in rows:
        target = session.get(User, row.blocked_user_id)
        if target:
            users.append({"blocked_at": row.created_at.isoformat(), **public_user(target)})
    return {"blocked": users}


@router.get("/status/{user_id}")
def block_status(user_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    mine = session.exec(
        select(Block).where(Block.user_id == user.id, Block.blocked_user_id == user_id)
    ).first()
    return {"blocked": bool(mine), "either": social.is_blocked(session, user.id, user_id)}


@router.post("/{user_id}")
def block_user(user_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """相手をブロックする。フォローとフレンド関係も同時に解消する。"""
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="自分自身はブロックできません。")
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    existing = session.exec(
        select(Block).where(Block.user_id == user.id, Block.blocked_user_id == user_id)
    ).first()
    if existing:
        return {"blocked": True}

    session.add(Block(user_id=user.id, blocked_user_id=user_id))

    for row in session.exec(
        select(Follow).where(Follow.follower_id == user.id, Follow.followee_id == user_id)
    ).all():
        session.delete(row)
    for row in session.exec(
        select(Follow).where(Follow.follower_id == user_id, Follow.followee_id == user.id)
    ).all():
        session.delete(row)

    for row in session.exec(select(Friendship)).all():
        pair = {row.requester_id, row.addressee_id}
        if pair == {user.id, user_id}:
            session.delete(row)

    session.commit()
    return {"blocked": True}


@router.post("/{user_id}/remove")
def unblock_user(user_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """ブロックを解除する。フォローやフレンド関係は元に戻らない。"""
    existing = session.exec(
        select(Block).where(Block.user_id == user.id, Block.blocked_user_id == user_id)
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
    return {"blocked": False}
