"""Me Talk グローバルチャット。

3本柱の3つ目。誰でも見られる短い投稿を流し、
いいね・返信・リポスト・フォローで広がっていく場所。

フィードは「すべて」と「フォロー中」の2種類。
ブロックした相手の投稿は表示されない。

Dev yuzuki_akrdev.ofc
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app import social
from app.models import Block, Follow, Post, PostLike, PostTag, User
from app.realtime import manager
from app.routes_auth import current_user, current_user_optional, public_user

router = APIRouter(prefix="/api/posts", tags=["posts"])
follow_router = APIRouter(prefix="/api/follows", tags=["follows"])

MAX_CONTENT = 500
MAX_ATTACHMENTS = 4
PAGE_SIZE = 30


class CreatePost(BaseModel):
    content: str = ""
    attachments: list[dict] = []
    reply_to: int | None = None
    repost_of: int | None = None


def _blocked_ids(session: Session, user_id: int | None) -> set:
    return social.blocked_ids(session, user_id)


def _hidden_authors(session: Session, viewer: User | None, authors: set) -> set:
    """ブロックと鍵アカウントを合わせて、見せてはいけない投稿者を返す。"""
    hidden = social.blocked_ids(session, viewer.id if viewer else None)
    for author_id in authors:
        if author_id in hidden:
            continue
        author = session.get(User, author_id)
        if author and not social.can_view_posts(session, viewer, author):
            hidden.add(author_id)
    return hidden


def _counts(session: Session, post_id: int) -> dict:
    likes = session.exec(select(PostLike).where(PostLike.post_id == post_id)).all()
    replies = session.exec(
        select(Post).where(Post.reply_to == post_id, Post.deleted == False)  # noqa: E712
    ).all()
    reposts = session.exec(
        select(Post).where(Post.repost_of == post_id, Post.deleted == False)  # noqa: E712
    ).all()
    return {"likes": len(likes), "replies": len(replies), "reposts": len(reposts)}


def _post_public(session: Session, post: Post, viewer: User | None, depth: int = 1) -> dict:
    author = session.get(User, post.author_id)
    try:
        attachments = json.loads(post.attachments) if post.attachments else []
    except (json.JSONDecodeError, TypeError):
        attachments = []
    counts = _counts(session, post.id)
    liked = False
    reposted = False
    if viewer:
        liked = bool(session.exec(
            select(PostLike).where(PostLike.post_id == post.id, PostLike.user_id == viewer.id)
        ).first())
        reposted = bool(session.exec(
            select(Post).where(
                Post.repost_of == post.id,
                Post.author_id == viewer.id,
                Post.deleted == False,  # noqa: E712
            )
        ).first())
    tags = [t.tag for t in session.exec(select(PostTag).where(PostTag.post_id == post.id)).all()]
    data = {
        "id": post.id,
        "tags": [] if post.deleted else tags,
        "private": bool(author.is_private) if author else False,
        "content": "" if post.deleted else post.content,
        "attachments": [] if post.deleted else attachments,
        "author": public_user(author) if author else None,
        "created_at": post.created_at.isoformat(),
        "deleted": bool(post.deleted),
        "reply_to": post.reply_to,
        "repost_of": post.repost_of,
        "counts": counts,
        "liked": liked,
        "reposted": reposted,
        "mine": bool(viewer and viewer.id == post.author_id),
    }
    if depth > 0 and post.repost_of:
        original = session.get(Post, post.repost_of)
        if original:
            data["original"] = _post_public(session, original, viewer, depth - 1)
    if depth > 0 and post.reply_to:
        parent = session.get(Post, post.reply_to)
        if parent:
            data["parent"] = _post_public(session, parent, viewer, 0)
    return data


def _clean_attachments(items: list) -> str:
    cleaned = []
    for a in items[:MAX_ATTACHMENTS]:
        if not isinstance(a, dict):
            continue
        url = str(a.get("url", ""))[:500]
        if not url:
            continue
        cleaned.append({
            "kind": str(a.get("kind", "file"))[:16],
            "url": url,
            "name": str(a.get("name", ""))[:120],
            "content_type": str(a.get("content_type", ""))[:80],
            "size": int(a.get("size", 0)) if str(a.get("size", "")).isdigit() else 0,
        })
    return json.dumps(cleaned, ensure_ascii=False)


@router.post("")
async def create_post(
    payload: CreatePost,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    content = (payload.content or "").strip()
    attachments = payload.attachments or []

    if payload.repost_of:
        original = session.get(Post, payload.repost_of)
        if not original or original.deleted:
            raise HTTPException(status_code=404, detail="元の投稿が見つかりません。")
        original_author = session.get(User, original.author_id)
        if not social.can_view_posts(session, user, original_author):
            raise HTTPException(status_code=403, detail="この投稿はリポストできません。")
        if original_author and original_author.is_private:
            raise HTTPException(status_code=403, detail="非公開アカウントの投稿はリポストできません。")
        if original.repost_of:
            payload.repost_of = original.repost_of
        if not content and not attachments:
            existing = session.exec(
                select(Post).where(
                    Post.repost_of == payload.repost_of,
                    Post.author_id == user.id,
                    Post.deleted == False,  # noqa: E712
                )
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail="すでにリポストしています。")
    elif not content and not attachments:
        raise HTTPException(status_code=400, detail="投稿が空です。")

    if len(content) > MAX_CONTENT:
        raise HTTPException(status_code=400, detail=f"投稿は{MAX_CONTENT}文字以内にしてください。")

    if payload.reply_to:
        parent = session.get(Post, payload.reply_to)
        if not parent or parent.deleted:
            raise HTTPException(status_code=404, detail="返信先の投稿が見つかりません。")
        parent_author = session.get(User, parent.author_id)
        if not social.can_view_posts(session, user, parent_author):
            raise HTTPException(status_code=403, detail="この投稿には返信できません。")

    post = Post(
        author_id=user.id,
        content=content,
        attachments=_clean_attachments(attachments),
        reply_to=payload.reply_to,
        repost_of=payload.repost_of,
    )
    session.add(post)
    session.commit()
    session.refresh(post)

    for tag in social.extract_tags(content):
        session.add(PostTag(post_id=post.id, tag=tag))
    session.commit()

    data = _post_public(session, post, user)
    if not user.is_private:
        await manager.broadcast_all({"type": "post.new", "data": data})

    from app.routes_notifications import create_notification
    if payload.reply_to:
        parent = session.get(Post, payload.reply_to)
        if parent:
            await create_notification(
                session, parent.author_id, "reply", f"{user.username} さんが返信しました",
                content[:60], actor_id=user.id, target_type="post", target_id=post.id,
            )
    elif payload.repost_of:
        original = session.get(Post, payload.repost_of)
        if original:
            await create_notification(
                session, original.author_id, "repost", f"{user.username} さんがリポストしました",
                (original.content or "")[:60], actor_id=user.id, target_type="post", target_id=original.id,
            )
    return data


@router.get("")
def list_posts(
    tab: str = "all",
    tag: str = "",
    before: int = 0,
    user: User | None = Depends(current_user_optional),
    session: Session = Depends(get_session),
):
    """タイムラインを返す。tab=all は全体、tab=following はフォロー中のみ。

    tag を指定するとそのタグが付いた投稿だけを返す。
    非公開アカウントの投稿は、相互フォローの相手にしか見えない。
    """
    query = select(Post).where(Post.reply_to == None)  # noqa: E711
    if tab == "following":
        if not user:
            raise HTTPException(status_code=401, detail="ログインが必要です。")
        ids = [f.followee_id for f in session.exec(
            select(Follow).where(Follow.follower_id == user.id)
        ).all()]
        ids.append(user.id)
        query = query.where(Post.author_id.in_(ids))
    if tag:
        keyword = tag.lstrip("#").lower()
        post_ids = [t.post_id for t in session.exec(
            select(PostTag).where(PostTag.tag == keyword)
        ).all()]
        if not post_ids:
            return {"tab": tab, "tag": keyword, "posts": [], "next_before": 0}
        query = query.where(Post.id.in_(post_ids))
    if before:
        query = query.where(Post.id < before)
    rows = session.exec(query.order_by(Post.id.desc()).limit(PAGE_SIZE * 3)).all()

    hidden = _hidden_authors(session, user, {p.author_id for p in rows})
    visible = [p for p in rows if p.author_id not in hidden][:PAGE_SIZE]
    return {
        "tab": tab,
        "tag": tag.lstrip("#").lower() if tag else "",
        "posts": [_post_public(session, p, user) for p in visible],
        "next_before": visible[-1].id if len(visible) == PAGE_SIZE else 0,
    }


@router.get("/tags/trending")
def trending_tags(session: Session = Depends(get_session)):
    """よく使われているタグを新しい順の重みで返す。"""
    rows = session.exec(select(PostTag).order_by(PostTag.id.desc()).limit(600)).all()
    counts = {}
    for row in rows:
        counts[row.tag] = counts.get(row.tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:20]
    return {"tags": [{"tag": t, "count": n} for t, n in ranked]}


@router.get("/{post_id}")
def get_post(
    post_id: int,
    user: User | None = Depends(current_user_optional),
    session: Session = Depends(get_session),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="投稿が見つかりません。")
    author = session.get(User, post.author_id)
    if not social.can_view_posts(session, user, author):
        raise HTTPException(status_code=403, detail="この投稿は表示できません。")
    hidden = _blocked_ids(session, user.id if user else None)
    replies = session.exec(
        select(Post).where(Post.reply_to == post_id).order_by(Post.id)
    ).all()
    return {
        "post": _post_public(session, post, user),
        "replies": [_post_public(session, r, user, 0) for r in replies if r.author_id not in hidden],
    }


@router.post("/{post_id}/like")
async def toggle_like(
    post_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    post = session.get(Post, post_id)
    if not post or post.deleted:
        raise HTTPException(status_code=404, detail="投稿が見つかりません。")
    author = session.get(User, post.author_id)
    if not social.can_view_posts(session, user, author):
        raise HTTPException(status_code=403, detail="この投稿にはいいねできません。")
    existing = session.exec(
        select(PostLike).where(PostLike.post_id == post_id, PostLike.user_id == user.id)
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
        liked = False
    else:
        session.add(PostLike(post_id=post_id, user_id=user.id))
        session.commit()
        liked = True
        from app.routes_notifications import create_notification
        await create_notification(
            session, post.author_id, "like", f"{user.username} さんがいいねしました",
            (post.content or "")[:60], actor_id=user.id, target_type="post", target_id=post.id,
        )
    counts = _counts(session, post_id)
    await manager.broadcast_all({"type": "post.counts", "data": {"id": post_id, "counts": counts}})
    return {"liked": liked, "counts": counts}


@router.post("/{post_id}/delete")
async def delete_post(
    post_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="投稿が見つかりません。")
    is_admin = "developer" in (user.badges or "").split(",")
    if post.author_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="自分の投稿のみ削除できます。")
    post.deleted = True
    post.content = ""
    post.attachments = "[]"
    session.add(post)
    session.commit()
    await manager.broadcast_all({"type": "post.deleted", "data": {"id": post_id}})
    return {"deleted": True}


@router.get("/user/{user_id}")
def user_posts(
    user_id: int,
    before: int = 0,
    user: User | None = Depends(current_user_optional),
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    if not social.can_view_posts(session, user, target):
        raise HTTPException(status_code=403, detail="このアカウントの投稿は表示できません。")
    query = select(Post).where(Post.author_id == user_id, Post.reply_to == None)  # noqa: E711
    if before:
        query = query.where(Post.id < before)
    rows = session.exec(query.order_by(Post.id.desc()).limit(PAGE_SIZE)).all()
    return {"posts": [_post_public(session, p, user) for p in rows]}


class FollowStat(BaseModel):
    pass


@follow_router.post("/{user_id}")
async def follow_user(
    user_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="自分自身はフォローできません。")
    target = session.get(User, user_id)
    if not target or not target.is_active:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    if social.is_blocked(session, user.id, user_id):
        raise HTTPException(status_code=403, detail="このユーザーはフォローできません。")
    existing = session.exec(
        select(Follow).where(Follow.follower_id == user.id, Follow.followee_id == user_id)
    ).first()
    if existing:
        return {"following": True}
    session.add(Follow(follower_id=user.id, followee_id=user_id))
    session.commit()
    from app.routes_notifications import create_notification
    await create_notification(
        session, user_id, "follow", f"{user.username} さんにフォローされました", "",
        actor_id=user.id, target_type="user", target_id=user.id,
    )
    return {"following": True}


@follow_router.post("/{user_id}/remove")
def unfollow_user(
    user_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    existing = session.exec(
        select(Follow).where(Follow.follower_id == user.id, Follow.followee_id == user_id)
    ).first()
    if existing:
        session.delete(existing)
        session.commit()
    return {"following": False}


@follow_router.get("/stats/{user_id}")
def follow_stats(
    user_id: int,
    user: User | None = Depends(current_user_optional),
    session: Session = Depends(get_session),
):
    followers = session.exec(select(Follow).where(Follow.followee_id == user_id)).all()
    following = session.exec(select(Follow).where(Follow.follower_id == user_id)).all()
    is_following = False
    if user:
        is_following = any(f.follower_id == user.id for f in followers)
    return {
        "followers": len(followers),
        "following": len(following),
        "is_following": is_following,
    }


@follow_router.get("/list/{user_id}")
def follow_list(
    user_id: int,
    kind: str = Query(default="followers"),
    user: User | None = Depends(current_user_optional),
    session: Session = Depends(get_session),
):
    if kind == "following":
        rows = session.exec(select(Follow).where(Follow.follower_id == user_id)).all()
        ids = [r.followee_id for r in rows]
    else:
        rows = session.exec(select(Follow).where(Follow.followee_id == user_id)).all()
        ids = [r.follower_id for r in rows]
    hidden = _blocked_ids(session, user.id if user else None)
    users = [session.get(User, i) for i in ids if i not in hidden]
    return {"kind": kind, "users": [public_user(u) for u in users if u]}
