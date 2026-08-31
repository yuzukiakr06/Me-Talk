"""Me Talk ユーザープロフィールルーター。

自分のプロフィール編集(バナー/背景/モード/アクセント色/自己紹介/ステータス)と、
他ユーザーのプロフィール表示、共通サーバー(オープンチャット)数などを提供する。
バナー画像は10MBまで(GIF可)。

Dev yuzuki_akrdev.ofc
"""

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.models import Block, OpenChatMember, Upload, User
from app.realtime import manager
from app.routes_auth import current_user, current_user_optional, public_user, validate_display_name

router = APIRouter(prefix="/api/profile", tags=["profile"])

BANNER_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
BANNER_DIR.mkdir(parents=True, exist_ok=True)

BANNER_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
BANNER_MAX = 10 * 1024 * 1024

HEX = set("0123456789abcdefABCDEF")


class UpdateProfile(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    status_text: str | None = None
    status_emoji: str | None = None
    presence: str | None = None
    is_private: bool | None = None
    pronouns: str | None = None
    avatar_url: str | None = None
    banner_url: str | None = None
    banner_color: str | None = None
    background_url: str | None = None
    profile_mode: str | None = None
    accent_color: str | None = None
    sns_spotify: str | None = None
    sns_x: str | None = None
    sns_instagram: str | None = None
    sns_youtube: str | None = None
    sns_discord: str | None = None


def _valid_color(value: str) -> bool:
    if not value:
        return True
    if not value.startswith("#"):
        return False
    body = value[1:]
    return len(body) in (3, 6) and all(ch in HEX for ch in body)


def _common_servers(session: Session, me: int, other: int) -> int:
    my_groups = {m.openchat_id for m in session.exec(select(OpenChatMember).where(OpenChatMember.user_id == me)).all()}
    other_groups = {m.openchat_id for m in session.exec(select(OpenChatMember).where(OpenChatMember.user_id == other)).all()}
    return len(my_groups & other_groups)


def _profile_payload(session: Session, target: User, viewer: User | None) -> dict:
    data = public_user(target, viewer)
    data["online"] = manager.is_online(target.id)
    if viewer and viewer.id != target.id:
        data["common_servers"] = _common_servers(session, viewer.id, target.id)
        data["blocked"] = session.exec(
            select(Block).where(Block.user_id == viewer.id, Block.blocked_user_id == target.id)
        ).first() is not None
        from app.routes_friends import friendship_state, _friend_ids
        data["friend_state"] = friendship_state(session, viewer.id, target.id)
        if data["friend_state"] == "friends":
            data["common_friends"] = len(_friend_ids(session, viewer.id) & _friend_ids(session, target.id) - {viewer.id, target.id})
        else:
            data["common_friends"] = 0
    else:
        data["common_servers"] = 0
        data["blocked"] = False
        data["friend_state"] = "self"
        data["common_friends"] = 0
    return data


@router.get("/me")
def get_my_profile(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return _profile_payload(session, user, user)


@router.patch("/me")
def update_my_profile(payload: UpdateProfile, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if payload.display_name is not None:
        user.display_name = validate_display_name(payload.display_name, user.username)
    if payload.bio is not None:
        user.bio = payload.bio.strip()[:1000]
    if payload.status_text is not None:
        user.status_text = payload.status_text.strip()[:100]
    if payload.status_emoji is not None:
        user.status_emoji = payload.status_emoji.strip()[:16]
    if payload.pronouns is not None:
        user.pronouns = payload.pronouns.strip()[:40]
    if payload.is_private is not None:
        user.is_private = bool(payload.is_private)
    if payload.presence is not None:
        if payload.presence not in ("online", "away", "busy", "invisible"):
            raise HTTPException(status_code=400, detail="オンライン状態が不正です。")
        user.presence = payload.presence
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url or None
    if payload.banner_url is not None:
        user.banner_url = payload.banner_url or None
    if payload.background_url is not None:
        user.background_url = payload.background_url or None
    if payload.banner_color is not None:
        if not _valid_color(payload.banner_color):
            raise HTTPException(status_code=400, detail="バナー色の形式が正しくありません。")
        user.banner_color = payload.banner_color or None
    if payload.accent_color is not None:
        if not _valid_color(payload.accent_color):
            raise HTTPException(status_code=400, detail="アクセント色の形式が正しくありません。")
        user.accent_color = payload.accent_color or None
    if payload.profile_mode is not None:
        if payload.profile_mode not in ("banner", "background"):
            raise HTTPException(status_code=400, detail="プロフィールモードが不正です。")
        user.profile_mode = payload.profile_mode
    for field, value in (
        ("sns_spotify", payload.sns_spotify),
        ("sns_x", payload.sns_x),
        ("sns_instagram", payload.sns_instagram),
        ("sns_youtube", payload.sns_youtube),
        ("sns_discord", payload.sns_discord),
    ):
        if value is not None:
            setattr(user, field, value.strip()[:200])
    session.add(user)
    session.commit()
    session.refresh(user)
    return _profile_payload(session, user, user)


@router.get("/{user_id}")
def get_user_profile(user_id: int, viewer: User = Depends(current_user), session: Session = Depends(get_session)):
    target = session.get(User, user_id)
    if not target or not target.is_active:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません。")
    return _profile_payload(session, target, viewer)


@router.post("/banner")
async def upload_banner(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    if file.content_type not in BANNER_TYPES:
        raise HTTPException(status_code=400, detail="対応していない画像形式です。png/jpeg/webp/gif に対応しています。")
    data = await file.read()
    if len(data) > BANNER_MAX:
        raise HTTPException(status_code=400, detail="ファイルサイズが大きすぎます(バナーは10MBまで)。")
    ext = BANNER_TYPES[file.content_type]
    name = f"{secrets.token_urlsafe(16)}{ext}"
    (BANNER_DIR / name).write_bytes(data)
    session.add(Upload(owner_id=user.id, filename=name, content_type=file.content_type, size=len(data)))
    session.commit()
    return {"url": f"/uploads/{name}", "size": len(data), "is_gif": file.content_type == "image/gif"}
