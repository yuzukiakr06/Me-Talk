"""MIRAI Chat データモデル。

設計書(mirai-chat-design.md)のデータモデルをそのまま実装する。
フェーズ1では User / KnownDevice / EmailCode / Session のみ実際に使うが、
将来のフェーズで再マイグレーションが不要なように全テーブルを先に定義しておく。

Dev yuzuki_akrdev.ofc
"""

from datetime import datetime
from typing import Optional

from app.crypto import EncryptedText

from sqlmodel import SQLModel, Field


def now() -> datetime:
    return datetime.utcnow()


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    user_id: Optional[str] = Field(default=None, index=True)
    display_name: str = ""
    email: str = Field(unique=True, index=True)
    email_verified: bool = False
    password_salt: Optional[str] = None
    password_hash: Optional[str] = None
    google_id: Optional[str] = Field(default=None, unique=True, index=True)
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    banner_color: Optional[str] = None
    background_url: Optional[str] = None
    profile_mode: str = "banner"
    accent_color: Optional[str] = None
    status_text: str = ""
    status_emoji: str = ""
    presence: str = "online"
    pronouns: str = ""
    badges: str = ""
    sns_spotify: str = ""
    sns_x: str = ""
    sns_instagram: str = ""
    sns_youtube: str = ""
    sns_discord: str = ""
    bio: str = ""
    is_active: bool = True
    is_private: bool = False
    tutorial_done: bool = False
    setup_done: bool = True
    setup_token: Optional[str] = None
    setup_token_expires: Optional[datetime] = None
    link_token: Optional[str] = None
    link_token_expires: Optional[datetime] = None
    totp_secret: Optional[str] = None
    totp_enabled: bool = False
    totp_enabled_at: Optional[datetime] = None
    registration_ip_hash: Optional[str] = None
    created_at: datetime = Field(default_factory=now)


class KnownDevice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    ip_hash: str
    ua_hash: str
    created_at: datetime = Field(default_factory=now)
    last_seen: datetime = Field(default_factory=now)


class EmailCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    purpose: str
    email: str = Field(index=True)
    code_hash: str
    expires_at: datetime
    tries: int = 0
    issue_count: int = 1
    created_at: datetime = Field(default_factory=now)


class Session(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime
    remember: bool = False


class OpenChat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    icon_url: Optional[str] = None
    cover_url: Optional[str] = None
    room_bg_url: Optional[str] = None
    owner_id: int = Field(index=True, foreign_key="user.id")
    visibility: str = "public"
    allow_anonymous: bool = False
    created_at: datetime = Field(default_factory=now)


class OpenChatMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    role: str = "member"
    anon_label: str = ""
    joined_at: datetime = Field(default_factory=now)


class OpenChatInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    code: str = Field(unique=True, index=True)
    created_by: int = Field(foreign_key="user.id")
    uses: int = 0
    max_uses: Optional[int] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class OpenChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    author_id: int = Field(index=True, foreign_key="user.id")
    content: str = Field(sa_type=EncryptedText)
    anonymous: bool = False
    anon_label: str = ""
    reply_to: Optional[int] = Field(default=None, foreign_key="openchatmessage.id")
    attachments: str = "[]"
    created_at: datetime = Field(default_factory=now)
    edited_at: Optional[datetime] = None
    deleted: bool = False


class DMConversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_a: int = Field(index=True, foreign_key="user.id")
    user_b: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class DMMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True, foreign_key="dmconversation.id")
    author_id: int = Field(index=True, foreign_key="user.id")
    content: str = Field(default="", sa_type=EncryptedText)
    attachments: str = "[]"
    reply_to: Optional[int] = None
    deleted: bool = False
    created_at: datetime = Field(default_factory=now)
    edited_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    fade_mode: Optional[str] = None
    fade_seconds: Optional[int] = None
    fade_at: Optional[datetime] = None


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: int = Field(index=True, foreign_key="user.id")
    content: str = Field(default="", sa_type=EncryptedText)
    attachments: str = "[]"
    reply_to: Optional[int] = Field(default=None, foreign_key="post.id")
    repost_of: Optional[int] = Field(default=None, foreign_key="post.id")
    deleted: bool = False
    created_at: datetime = Field(default_factory=now)


class PostLike(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(index=True, foreign_key="post.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class Follow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    follower_id: int = Field(index=True, foreign_key="user.id")
    followee_id: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class Block(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    blocked_user_id: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class Report(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    reporter_id: int = Field(index=True, foreign_key="user.id")
    target_type: str
    target_id: int
    reason: str
    created_at: datetime = Field(default_factory=now)


class OpenChatEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    author_id: int = Field(foreign_key="user.id")
    title: str
    body: str = ""
    starts_at: datetime
    ends_at: Optional[datetime] = None
    all_day: bool = False
    created_at: datetime = Field(default_factory=now)


class OpenChatNote(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    author_id: int = Field(foreign_key="user.id")
    title: str
    body: str = Field(default="", sa_type=EncryptedText)
    pinned: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Upload(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    owner_id: int = Field(index=True, foreign_key="user.id")
    filename: str
    content_type: str
    size: int
    created_at: datetime = Field(default_factory=now)


class OpenChatMute(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class Friendship(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    requester_id: int = Field(index=True, foreign_key="user.id")
    addressee_id: int = Field(index=True, foreign_key="user.id")
    status: str = "pending"
    created_at: datetime = Field(default_factory=now)
    accepted_at: Optional[datetime] = None


class MessageHidden(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    scope: str = "openchat"
    message_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=now)


class OpenChatPin(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    openchat_id: int = Field(index=True, foreign_key="openchat.id")
    message_id: int = Field(foreign_key="openchatmessage.id")
    pinned_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=now)


class DMAttachmentView(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(index=True, foreign_key="dmmessage.id")
    attachment_index: int = 0
    viewer_id: int = Field(foreign_key="user.id")
    viewed_at: datetime = Field(default_factory=now)


class Notification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    kind: str = "system"
    title: str = ""
    body: str = ""
    actor_id: Optional[int] = None
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    read: bool = False
    created_at: datetime = Field(default_factory=now)


class MessageReaction(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = "openchat"
    message_id: int = Field(index=True)
    user_id: int = Field(foreign_key="user.id")
    emoji: str = ""
    created_at: datetime = Field(default_factory=now)


class SupportTicket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    kind: str = "bug"
    subject: str = ""
    body: str = ""
    contact_email: str = ""
    app_version: str = ""
    client_info: str = ""
    status: str = Field(default="open", index=True)
    admin_note: str = ""
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class BackupCode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    code_hash: str = Field(index=True)
    used_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class Passkey(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    credential_id: str = Field(index=True)
    public_key: str = ""
    sign_count: int = 0
    transports: str = ""
    name: str = ""
    created_at: datetime = Field(default_factory=now)
    last_used_at: Optional[datetime] = None


class WebAuthnChallenge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    challenge: str = Field(index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    purpose: str = "login"
    expires_at: datetime
    created_at: datetime = Field(default_factory=now)


class ScheduledMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    author_id: int = Field(index=True, foreign_key="user.id")
    scope: str = "openchat"
    target_id: int = Field(index=True)
    content: str = Field(default="", sa_type=EncryptedText)
    send_at: datetime = Field(index=True)
    status: str = Field(default="pending", index=True)
    error: str = ""
    message_id: Optional[int] = None
    created_at: datetime = Field(default_factory=now)
    sent_at: Optional[datetime] = None


class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    scope: str = "openchat"
    target_id: Optional[int] = Field(default=None, index=True)
    text: str = Field(default="", sa_type=EncryptedText)
    remind_at: datetime = Field(index=True)
    post_in_chat: bool = True
    status: str = Field(default="pending", index=True)
    source_message_id: Optional[int] = None
    created_at: datetime = Field(default_factory=now)
    fired_at: Optional[datetime] = None


class PostTag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(index=True, foreign_key="post.id")
    tag: str = Field(index=True)
    created_at: datetime = Field(default_factory=now)


class CallSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    mode: str = "mesh"
    scope: str = "dm"
    target_id: int = Field(index=True)
    started_by: int = Field(index=True, foreign_key="user.id")
    status: str = Field(default="active", index=True)
    video: bool = False
    created_at: datetime = Field(default_factory=now)
    ended_at: Optional[datetime] = None


class CallParticipant(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    call_id: int = Field(index=True, foreign_key="callsession.id")
    user_id: int = Field(index=True, foreign_key="user.id")
    muted: bool = False
    deafened: bool = False
    video_on: bool = False
    screen_on: bool = False
    listen_only: bool = False
    sfu_session_id: str = ""
    audio_track: str = ""
    video_track: str = ""
    joined_at: datetime = Field(default_factory=now)
    left_at: Optional[datetime] = None
