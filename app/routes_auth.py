"""MIRAI Chat 認証ルーター。

会員登録・ログイン・新規端末確認・Googleログイン・ログアウトを扱う。
MIRAI ID と同じ流れ:
  - 会員登録は先に /email-code でコードを送り、/register でコードを添えて確定する。
  - ログインは、既知の端末(IP+UAの組み合わせ)なら即ログイン、未知の端末ならメール確認コードを要求する。

Dev yuzuki_akrdev.ofc
"""

import json
import re
from urllib.parse import urlencode
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select

from app import config, ids, security
from app.db import get_session
from app.email_util import send_verification_code
from app.models import EmailCode, KnownDevice, Session as SessionModel, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(
    r"^[a-zA-Z0-9_.]{%d,%d}$" % (config.USERNAME_MIN_LENGTH, config.USERNAME_MAX_LENGTH)
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_ALLOWED_RE = re.compile(r"^[\x21-\x7E]+$")

DISPLAY_NAME_MAX = 32

DISPLAY_NAME_BANNED = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)


def validate_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"ユーザー名は英数字とアンダースコア( _ )とピリオド( . )で{config.USERNAME_MIN_LENGTH}〜{config.USERNAME_MAX_LENGTH}文字にしてください。",
        )
    if value.startswith(".") or value.endswith("."):
        raise HTTPException(status_code=400, detail="ユーザー名の最初と最後にピリオドは使えません。")
    if ".." in value:
        raise HTTPException(status_code=400, detail="ピリオドを連続して使うことはできません。")
    return value


def validate_display_name(value: str | None, fallback: str) -> str:
    """表示名を検証する。

    絵文字や記号や装飾文字はそのまま通す。
    制御文字と、表示を偽装できる不可視文字(ゼロ幅・書字方向の上書き)だけを取り除く。
    空になった場合は fallback を使う。
    """
    cleaned = DISPLAY_NAME_BANNED.sub("", value or "").strip()
    if not cleaned:
        return fallback
    if len(cleaned) > DISPLAY_NAME_MAX:
        raise HTTPException(status_code=400, detail=f"表示名は{DISPLAY_NAME_MAX}文字以内にしてください。")
    return cleaned


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise HTTPException(status_code=400, detail="メールアドレスの形式が正しくありません。")
    return value


def validate_password(value: str) -> str:
    if len(value) < 8:
        raise HTTPException(status_code=400, detail="パスワードは8文字以上にしてください。")
    if len(value) > 30:
        raise HTTPException(status_code=400, detail="パスワードは30文字以内にしてください。")
    if not PASSWORD_ALLOWED_RE.match(value):
        raise HTTPException(status_code=400, detail="パスワードに使えない文字が含まれています。半角の英数字と記号のみ使用できます。")
    if not re.search(r"[a-z]", value):
        raise HTTPException(status_code=400, detail="パスワードに小文字の英字を含めてください。")
    if not re.search(r"[A-Z]", value):
        raise HTTPException(status_code=400, detail="パスワードに大文字の英字を含めてください。")
    if not re.search(r"[0-9]", value):
        raise HTTPException(status_code=400, detail="パスワードに数字を含めてください。")
    return value


class EmailCodeRequest(BaseModel):
    email: str
    purpose: str = "register"
    turnstile_token: str | None = None


class RegisterRequest(BaseModel):
    username: str
    display_name: str | None = None
    email: str
    password: str
    email_code: str
    accepted_terms: bool

    @field_validator("accepted_terms")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("利用規約への同意が必要です。")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str
    email_code: str | None = None
    totp_code: str | None = None
    backup_code: str | None = None
    remember: bool = True
    turnstile_token: str | None = None


def _check_turnstile(token: str | None, request: Request) -> None:
    """ロボット確認を検証する。失敗なら400を返す。"""
    from app import turnstile
    if not turnstile.enabled():
        return
    client_ip = request.client.host if request.client else ""
    if not turnstile.verify(token or "", client_ip):
        raise HTTPException(status_code=400, detail="ロボットではないことの確認に失敗しました。もう一度お試しください。")


def new_user_id(session: Session) -> str:
    """既存と衝突しないユーザーIDを採番する。"""
    used = {u.user_id for u in session.exec(select(User)).all() if u.user_id}
    return ids.generate_unique(lambda value: value in used)


def _code_key(purpose: str, email: str) -> str:
    return f"{purpose}:{email.lower()}"


def _issue_code(session: Session, purpose: str, email: str) -> str:
    existing = session.exec(
        select(EmailCode).where(EmailCode.purpose == purpose, EmailCode.email == email.lower())
    ).first()
    if existing and security.not_expired(existing.expires_at):
        wait_ok_at = existing.created_at + timedelta(seconds=60)
        if datetime.utcnow() < wait_ok_at:
            wait = int((wait_ok_at - datetime.utcnow()).total_seconds())
            raise HTTPException(status_code=429, detail=f"認証コードは{max(wait, 1)}秒後に再送信できます。")
        if existing.issue_count >= 5:
            raise HTTPException(status_code=429, detail="認証コードの再送信回数が上限に達しました。時間をおいて再度お試しください。")
    code = security.make_code()
    if existing:
        existing.code_hash = security.code_hash(code)
        existing.expires_at = security.code_expires_at()
        existing.tries = 0
        existing.issue_count += 1
        existing.created_at = datetime.utcnow()
        session.add(existing)
    else:
        session.add(EmailCode(
            purpose=purpose,
            email=email.lower(),
            code_hash=security.code_hash(code),
            expires_at=security.code_expires_at(),
        ))
    session.commit()
    return code


def _verify_code(session: Session, purpose: str, email: str, code: str) -> None:
    item = session.exec(
        select(EmailCode).where(EmailCode.purpose == purpose, EmailCode.email == email.lower())
    ).first()
    if not item or not security.not_expired(item.expires_at):
        raise HTTPException(status_code=400, detail="認証コードを送信し直してください。")
    if item.tries >= 5:
        raise HTTPException(status_code=429, detail="認証コードの入力回数が多すぎます。再送信してください。")
    if item.code_hash != security.code_hash(code):
        item.tries += 1
        session.add(item)
        session.commit()
        raise HTTPException(status_code=400, detail="認証コードが違います。")
    session.delete(item)
    session.commit()


@router.post("/email-code")
def request_email_code(payload: EmailCodeRequest, request: Request, session: Session = Depends(get_session)):
    _check_turnstile(payload.turnstile_token, request)
    email = validate_email(payload.email)
    if payload.purpose == "register":
        exists = session.exec(select(User).where(User.email == email)).first()
        if exists:
            raise HTTPException(status_code=409, detail="このメールアドレスはすでに登録されています。")
    code = _issue_code(session, payload.purpose, email)
    sent = send_verification_code(email, code, payload.purpose)
    body = {"sent": sent}
    if not config.HIDE_DEV_CODES:
        body["dev_code"] = code
    return body


@router.post("/register")
def register(payload: RegisterRequest, request: Request, session: Session = Depends(get_session)):
    username = validate_username(payload.username)
    display_name = validate_display_name(payload.display_name, username)
    email = validate_email(payload.email)
    password = validate_password(payload.password)

    existing = session.exec(select(User)).all()
    lowered = username.lower()
    if any((u.username or "").lower() == lowered for u in existing):
        raise HTTPException(status_code=409, detail="このユーザー名はすでに使われています。")
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=409, detail="このメールアドレスはすでに登録されています。")

    _verify_code(session, "register", email, payload.email_code)

    salt, digest = security.hash_password(password)
    ip_hash, _ = security.request_fingerprint(request)
    from app.badges import initial_badges
    badges = initial_badges(username)
    user = User(
        username=username,
        user_id=new_user_id(session),
        display_name=display_name,
        email=email,
        email_verified=True,
        password_salt=salt,
        password_hash=digest,
        registration_ip_hash=ip_hash,
        badges=",".join(badges),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"registered": True, "username": user.username}


def _set_session_cookie(response: Response, token: str, remember: bool) -> None:
    max_age = 60 * 60 * 24 * 30 if remember else None
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=config.BASE_URL.startswith("https"),
        max_age=max_age,
        path="/",
    )


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    return request.cookies.get(config.SESSION_COOKIE_NAME)


def _known_device(session: Session, user_id: int, ip_hash: str, ua_hash: str) -> bool:
    if not ip_hash:
        return False
    device = session.exec(
        select(KnownDevice).where(
            KnownDevice.user_id == user_id,
            KnownDevice.ip_hash == ip_hash,
            KnownDevice.ua_hash == ua_hash,
        )
    ).first()
    if device:
        device.last_seen = datetime.utcnow()
        session.add(device)
        session.commit()
        return True
    return False


def _remember_device(session: Session, user_id: int, ip_hash: str, ua_hash: str) -> None:
    if not ip_hash:
        return
    existing = session.exec(
        select(KnownDevice).where(
            KnownDevice.user_id == user_id,
            KnownDevice.ip_hash == ip_hash,
            KnownDevice.ua_hash == ua_hash,
        )
    ).first()
    if existing:
        existing.last_seen = datetime.utcnow()
        session.add(existing)
        session.commit()
        return
    session.add(KnownDevice(user_id=user_id, ip_hash=ip_hash, ua_hash=ua_hash))
    devices = session.exec(
        select(KnownDevice).where(KnownDevice.user_id == user_id).order_by(KnownDevice.last_seen)
    ).all()
    if len(devices) > 10:
        for extra in devices[: len(devices) - 10]:
            session.delete(extra)
    session.commit()


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    if not payload.email_code:
        _check_turnstile(payload.turnstile_token, request)
    email = validate_email(payload.email)
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not security.verify_password(payload.password, user.password_salt, user.password_hash):
        raise HTTPException(status_code=401, detail="メールアドレスまたはパスワードが違います。")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="このアカウントは利用できません。")
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="メール認証が完了していません。")

    ip_hash, ua_hash = security.request_fingerprint(request)

    if not payload.email_code:
        if _known_device(session, user.id, ip_hash, ua_hash):
            pass
        else:
            code = _issue_code(session, "new_ip", email)
            sent = send_verification_code(email, code, "new_ip")
            body = {"verification_required": True, "sent": sent, "reason": "new_ip"}
            if not config.HIDE_DEV_CODES:
                body["dev_code"] = code
            raise HTTPException(status_code=401, detail=body)
    else:
        _verify_code(session, "new_ip", email, payload.email_code)
        _remember_device(session, user.id, ip_hash, ua_hash)

    _check_second_factor(session, user, payload.totp_code, payload.backup_code)

    token = security.new_session_token()
    expires_at = datetime.utcnow() + timedelta(days=30 if payload.remember else 1)
    session.add(SessionModel(token=token, user_id=user.id, expires_at=expires_at, remember=payload.remember))
    session.commit()
    _set_session_cookie(response, token, payload.remember)
    return {"authenticated": True, "token": token, "user": public_user(user, user)}


@router.post("/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    token = _extract_token(request)
    if token:
        record = session.get(SessionModel, token)
        if record:
            session.delete(record)
            session.commit()
    response.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return {"logged_out": True}


def public_user(user: User, viewer: User | None = None) -> dict:
    """公開してよいユーザー情報を組み立てる。

    ユーザーIDは識別用の内部値なので、開発者バッジを持つ閲覧者にだけ含める。
    """
    data = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "banner_url": user.banner_url,
        "banner_color": user.banner_color,
        "background_url": user.background_url,
        "profile_mode": user.profile_mode,
        "accent_color": user.accent_color,
        "status_text": user.status_text,
        "status_emoji": user.status_emoji,
        "presence": user.presence or "online",
        "pronouns": user.pronouns,
        "badges": [b for b in (user.badges or "").split(",") if b],
        "is_admin": "developer" in (user.badges or "").split(","),
        "sns_spotify": user.sns_spotify,
        "sns_x": user.sns_x,
        "sns_instagram": user.sns_instagram,
        "sns_youtube": user.sns_youtube,
        "sns_discord": user.sns_discord,
        "bio": user.bio,
        "is_private": bool(user.is_private),
        "created_at": user.created_at.isoformat(),
    }
    if viewer is not None and "developer" in (viewer.badges or "").split(","):
        data["user_id"] = user.user_id or ""
    return data


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    record = session.get(SessionModel, token)
    if not record or not security.not_expired(record.expires_at):
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    user = session.get(User, record.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="ログインが必要です。")
    return user


def current_user_optional(request: Request, session: Session = Depends(get_session)) -> User | None:
    token = _extract_token(request)
    if not token:
        return None
    record = session.get(SessionModel, token)
    if not record or not security.not_expired(record.expires_at):
        return None
    return session.get(User, record.user_id)


@router.get("/me")
def me(user: User = Depends(current_user)):
    data = public_user(user, user)
    data["tutorial_done"] = user.tutorial_done
    data["email_masked"] = _mask_email(user.email)
    data["has_password"] = has_password(user)
    data["google_linked"] = bool(user.google_id)
    return data


def _mask_email(email: str) -> str:
    """メールアドレスの一部を伏せて返す。"""
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        hidden = local[0] + "*"
    else:
        hidden = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{hidden}@{domain}"


@router.get("/username-available")
def username_available(username: str, session: Session = Depends(get_session)):
    value = (username or "").strip()
    if not USERNAME_RE.match(value):
        return {"available": False, "reason": "format"}
    if value.startswith(".") or value.endswith(".") or ".." in value:
        return {"available": False, "reason": "format"}
    lowered = value.lower()
    taken = any((u.username or "").lower() == lowered for u in session.exec(select(User)).all())
    return {"available": not taken, "reason": "taken" if taken else ""}


def has_password(user: User) -> bool:
    return bool(user.password_salt and user.password_hash)


def verify_identity(session: Session, user: User, purpose: str, password: str | None, email_code: str | None) -> None:
    """重要な変更の前に本人であることを確認する。

    パスワードを設定済みならパスワードで、
    Googleログインのみでパスワードが無いアカウントはメール確認コードで確認する。
    """
    if has_password(user):
        if not password:
            raise HTTPException(status_code=400, detail="現在のパスワードを入力してください。")
        if not security.verify_password(password, user.password_salt, user.password_hash):
            raise HTTPException(status_code=401, detail="現在のパスワードが違います。")
        return
    if not email_code:
        raise HTTPException(status_code=400, detail="メールに送った確認コードを入力してください。")
    _verify_code(session, purpose, user.email, email_code)


@router.post("/verify-code")
def send_verify_code(
    purpose: str,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """設定変更のための確認コードを、ログイン中のユーザー宛に送る。"""
    allowed = {"password_change", "password_set", "username_change", "totp_setup", "totp_disable", "totp_backup", "passkey_delete"}
    if purpose not in allowed:
        raise HTTPException(status_code=400, detail="用途が不正です。")
    if purpose == "password_change" and not has_password(user):
        purpose = "password_set"
    if purpose == "password_set" and has_password(user):
        purpose = "password_change"
    code = _issue_code(session, purpose, user.email)
    sent = send_verification_code(user.email, code, purpose)
    body = {"sent": sent, "purpose": purpose, "email_masked": _mask_email(user.email)}
    if not config.HIDE_DEV_CODES:
        body["dev_code"] = code
    return body


class PasswordChangeRequest(BaseModel):
    current_password: str | None = None
    email_code: str
    new_password: str


@router.patch("/password")
def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """パスワードを変更する。メール確認コードを必ず求める。"""
    new_password = validate_password(payload.new_password)
    purpose = "password_change" if has_password(user) else "password_set"
    if has_password(user):
        if not payload.current_password:
            raise HTTPException(status_code=400, detail="現在のパスワードを入力してください。")
        if not security.verify_password(payload.current_password, user.password_salt, user.password_hash):
            raise HTTPException(status_code=401, detail="現在のパスワードが違います。")
        if payload.current_password == new_password:
            raise HTTPException(status_code=400, detail="現在と違うパスワードにしてください。")
    _verify_code(session, purpose, user.email, payload.email_code)
    salt, digest = security.hash_password(new_password)
    user.password_salt = salt
    user.password_hash = digest
    session.add(user)
    session.commit()
    return {"changed": True, "has_password": True}


class UsernameChangeRequest(BaseModel):
    username: str
    password: str | None = None
    email_code: str | None = None


@router.patch("/username")
def change_username(
    payload: UsernameChangeRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """ユーザー名を変更する。本人確認を必ず求める。"""
    username = validate_username(payload.username)
    if username != user.username:
        verify_identity(session, user, "username_change", payload.password, payload.email_code)
    if username == user.username:
        return {"changed": False, "username": user.username}
    lowered = username.lower()
    others = session.exec(select(User)).all()
    if any((u.username or "").lower() == lowered and u.id != user.id for u in others):
        raise HTTPException(status_code=409, detail="このユーザー名はすでに使われています。")
    from app.badges import badge_list, add_badge, remove_badge, _is_configured_developer
    user.username = username
    if _is_configured_developer(username):
        add_badge(user, "developer")
    elif "developer" in badge_list(user):
        remove_badge(user, "developer")
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"changed": True, "username": user.username, "user": public_user(user, user)}


@router.post("/tutorial-done")
def tutorial_done(user: User = Depends(current_user), session: Session = Depends(get_session)):
    user.tutorial_done = True
    session.add(user)
    session.commit()
    return {"tutorial_done": True}


class GoogleSetupRequest(BaseModel):
    email: str
    setup_token: str
    username: str
    display_name: str | None = None


@router.post("/google/setup")
def google_setup(payload: GoogleSetupRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    """Google新規登録の初回設定。ユーザー名と表示名を確定してから確認コードへ進む。"""
    email = validate_email(payload.email)
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not user.google_id:
        raise HTTPException(status_code=404, detail="Googleアカウントが見つかりません。")
    if user.setup_done:
        raise HTTPException(status_code=400, detail="このアカウントの初期設定はすでに完了しています。")
    if not user.setup_token or not payload.setup_token or payload.setup_token != user.setup_token:
        raise HTTPException(status_code=403, detail="設定用の情報が正しくありません。もう一度Googleログインからやり直してください。")
    if not user.setup_token_expires or not security.not_expired(user.setup_token_expires):
        raise HTTPException(status_code=403, detail="時間が経過したため設定できませんでした。もう一度Googleログインからやり直してください。")

    username = validate_username(payload.username)
    lowered = username.lower()
    others = session.exec(select(User)).all()
    if any((u.username or "").lower() == lowered and u.id != user.id for u in others):
        raise HTTPException(status_code=409, detail="このユーザー名はすでに使われています。")

    from app.badges import initial_badges
    user.username = username
    user.display_name = validate_display_name(payload.display_name, username)
    user.badges = ",".join(initial_badges(username))
    user.setup_done = True
    user.setup_token = None
    user.setup_token_expires = None
    session.add(user)
    session.commit()
    session.refresh(user)

    ip_hash, ua_hash = security.request_fingerprint(request)
    if _known_device(session, user.id, ip_hash, ua_hash):
        token = security.new_session_token()
        expires_at = datetime.utcnow() + timedelta(days=30)
        session.add(SessionModel(token=token, user_id=user.id, expires_at=expires_at, remember=True))
        session.commit()
        _set_session_cookie(response, token, True)
        return {"authenticated": True, "token": token, "user": public_user(user, user)}

    code = _issue_code(session, "new_ip", email)
    sent = send_verification_code(email, code, "new_ip")
    body = {"authenticated": False, "verification_required": True, "sent": sent}
    if not config.HIDE_DEV_CODES:
        body["dev_code"] = code
    return body


def _check_second_factor(session: Session, user: User, totp_code: str | None, backup_code: str | None) -> None:
    """2段階認証が有効なら、認証アプリのコードかバックアップコードを確認する。"""
    from app.routes_2fa import check_second_factor
    check_second_factor(session, user, totp_code, backup_code)


class GoogleVerifyRequest(BaseModel):
    email: str
    email_code: str | None = None
    totp_code: str | None = None
    backup_code: str | None = None


@router.post("/google/verify")
def google_verify(payload: GoogleVerifyRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    """Googleログイン後の確認コードを検証してセッションを発行する。"""
    email = validate_email(payload.email)
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not user.google_id:
        raise HTTPException(status_code=404, detail="Googleアカウントが見つかりません。")
    if not user.setup_done:
        raise HTTPException(status_code=403, detail="先にユーザー名と表示名を設定してください。")
    ip_hash, ua_hash = security.request_fingerprint(request)
    if _known_device(session, user.id, ip_hash, ua_hash):
        pass
    else:
        if not payload.email_code:
            raise HTTPException(status_code=400, detail="メールに送った確認コードを入力してください。")
        _verify_code(session, "new_ip", email, payload.email_code)
        _remember_device(session, user.id, ip_hash, ua_hash)
    _check_second_factor(session, user, payload.totp_code, payload.backup_code)
    token = security.new_session_token()
    expires_at = datetime.utcnow() + timedelta(days=30)
    session.add(SessionModel(token=token, user_id=user.id, expires_at=expires_at, remember=True))
    session.commit()
    _set_session_cookie(response, token, True)
    return {"authenticated": True, "token": token, "user": public_user(user, user)}


@router.get("/config")
def auth_config():
    """ログイン画面が必要とする設定を返す。"""
    from app import turnstile
    return {
        "google_enabled": bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        "turnstile_enabled": turnstile.enabled(),
        "turnstile_site_key": config.TURNSTILE_SITE_KEY if turnstile.enabled() else "",
    }


@router.post("/google/link-start")
def google_link_start(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """ログイン中のアカウントにGoogleを連携するための認証URLを返す。"""
    if not config.GOOGLE_CLIENT_ID or not config.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Googleログインは現在設定されていません。")
    if user.google_id:
        raise HTTPException(status_code=400, detail="すでにGoogleアカウントと連携しています。")
    link_token = security.new_session_token()
    user.link_token = link_token
    user.link_token_expires = datetime.utcnow() + timedelta(minutes=15)
    session.add(user)
    session.commit()
    params = urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": link_token,
    })
    return {"url": f"https://accounts.google.com/o/oauth2/v2/auth?{params}"}


@router.post("/google/unlink")
def google_unlink(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Google連携を解除する。パスワード未設定の場合はログイン手段が無くなるため拒否する。"""
    if not user.google_id:
        raise HTTPException(status_code=400, detail="Googleアカウントとは連携していません。")
    if not has_password(user):
        raise HTTPException(
            status_code=400,
            detail="連携を解除するとログインできなくなります。先にパスワードを設定してください。",
        )
    user.google_id = None
    session.add(user)
    session.commit()
    return {"linked": False}


@router.get("/google/start")
def google_start(redirect: bool = False):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Googleログインは現在設定されていません。")
    params = urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
    })
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
    if redirect:
        return RedirectResponse(url=url)
    return {"url": url}


@router.get("/google/callback")
def google_callback(code: str, request: Request, response: Response, state: str = "", session: Session = Depends(get_session)):
    if not config.GOOGLE_CLIENT_ID or not config.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Googleログインは現在設定されていません。")
    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": config.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Google認証に失敗しました。")
    access_token = token_resp.json().get("access_token")
    info_resp = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if info_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Googleユーザー情報の取得に失敗しました。")
    info = info_resp.json()
    google_id = info.get("sub")
    email = (info.get("email") or "").lower()
    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Googleアカウント情報が不完全です。")

    if state:
        owner = session.exec(select(User).where(User.link_token == state)).first()
        if not owner or not owner.link_token_expires or not security.not_expired(owner.link_token_expires):
            return _google_result_page(authenticated=False, token="", email=email, link_error="時間が経過したため連携できませんでした。もう一度お試しください。")
        taken = session.exec(select(User).where(User.google_id == google_id)).first()
        if taken and taken.id != owner.id:
            return _google_result_page(authenticated=False, token="", email=email, link_error="このGoogleアカウントは別のMe Talkアカウントで使われています。")
        owner.google_id = google_id
        owner.link_token = None
        owner.link_token_expires = None
        if not owner.avatar_url and info.get("picture"):
            owner.avatar_url = info.get("picture")
        session.add(owner)
        session.commit()
        return _google_result_page(authenticated=False, token="", email=email, linked=True)

    created = False
    user = session.exec(select(User).where(User.google_id == google_id)).first()
    if not user:
        user = session.exec(select(User).where(User.email == email)).first()
        if user:
            user.google_id = google_id
        else:
            created = True
            limit = config.USERNAME_MAX_LENGTH
            base_username = re.sub(r"[^a-zA-Z0-9_.]", "", email.split("@")[0])[:limit] or "user"
            base_username = base_username.strip(".")[:limit] or "user"
            if len(base_username) < config.USERNAME_MIN_LENGTH:
                base_username = (base_username + "user")[:limit]
            username = base_username
            suffix = 1
            while session.exec(select(User).where(User.username == username)).first():
                suffix += 1
                tail = str(suffix)
                username = base_username[: max(1, limit - len(tail))] + tail
            from app.badges import initial_badges
            user = User(
                username=username,
                user_id=new_user_id(session),
                display_name=info.get("name") or username,
                email=email,
                email_verified=True,
                google_id=google_id,
                avatar_url=info.get("picture"),
                badges=",".join(initial_badges(username)),
                setup_done=False,
            )
        session.add(user)
        session.commit()
        session.refresh(user)

    if created or not user.setup_done:
        setup_token = security.new_session_token()
        user.setup_token = setup_token
        user.setup_token_expires = datetime.utcnow() + timedelta(minutes=30)
        user.setup_done = False
        session.add(user)
        session.commit()
        return _google_result_page(
            authenticated=False,
            token="",
            email=email,
            setup_required=True,
            setup_token=setup_token,
            suggested_username=user.username,
            suggested_display_name=validate_display_name(info.get("name"), user.username),
        )

    ip_hash, ua_hash = security.request_fingerprint(request)
    known = _known_device(session, user.id, ip_hash, ua_hash)
    if known and not user.totp_enabled:
        token = security.new_session_token()
        expires_at = datetime.utcnow() + timedelta(days=30)
        session.add(SessionModel(token=token, user_id=user.id, expires_at=expires_at, remember=True))
        session.commit()
        _set_session_cookie(response, token, True)
        return _google_result_page(authenticated=True, token=token, email=email)

    if known:
        return _google_result_page(authenticated=False, token="", email=email, totp_required=True, device_known=True)

    code = _issue_code(session, "new_ip", email)
    send_verification_code(email, code, "new_ip")
    return _google_result_page(
        authenticated=False,
        token="",
        email=email,
        dev_code=None if config.HIDE_DEV_CODES else code,
        totp_required=bool(user.totp_enabled),
    )


def _google_result_page(
    authenticated: bool,
    token: str,
    email: str,
    dev_code: str | None = None,
    setup_required: bool = False,
    setup_token: str = "",
    suggested_username: str = "",
    suggested_display_name: str = "",
    linked: bool = False,
    link_error: str = "",
    totp_required: bool = False,
    device_known: bool = False,
) -> HTMLResponse:
    """Googleログインの結果を親ウィンドウへ渡して閉じるページ。"""
    payload = {
        "source": "me-talk-google",
        "authenticated": authenticated,
        "token": token,
        "email": email,
    }
    if setup_required:
        payload["setup_required"] = True
        payload["setup_token"] = setup_token
        payload["suggested_username"] = suggested_username
        payload["suggested_display_name"] = suggested_display_name
    if totp_required:
        payload["totp_required"] = True
    if device_known:
        payload["device_known"] = True
    if linked:
        payload["linked"] = True
    if link_error:
        payload["link_error"] = link_error
    if dev_code:
        payload["dev_code"] = dev_code
    body = json.dumps(payload, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><title>Me Talk</title>
<style>body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0f14;color:#8b93a1;font-family:sans-serif;font-size:14px}}</style>
</head><body>
<div>Me Talk に戻っています...</div>
<script>
var data = {body};
try {{
    if (window.opener) {{
        window.opener.postMessage(data, "*");
        window.close();
    }} else {{
        sessionStorage.setItem("me_talk_google", JSON.stringify(data));
        location.replace("/app");
    }}
}} catch (e) {{
    location.replace("/app");
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)
