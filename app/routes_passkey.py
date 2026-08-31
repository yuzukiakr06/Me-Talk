"""Me Talk パスキー(WebAuthn)ルーター。

指紋・顔認証・端末のPINでログインできるようにする。
秘密鍵は端末から出ないため、パスワードのように盗まれたり
偽サイトに入力させられたりしない。

webauthn ライブラリが入っていない環境では機能ごと無効になり、
アプリ本体の起動を妨げない作りにしている。

Dev yuzuki_akrdev.ofc
"""

import base64
import json
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config, security
from app.db import get_session
from app.models import Passkey, Session as SessionModel, User, WebAuthnChallenge
from app.routes_auth import (
    _remember_device,
    _set_session_cookie,
    current_user,
    public_user,
    verify_identity,
)

router = APIRouter(prefix="/api/auth/passkey", tags=["passkey"])

CHALLENGE_MINUTES = 5
NAME_MAX = 40

try:
    import webauthn as _webauthn
    from webauthn.helpers import options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    _webauthn = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)
    print(f"[passkey] webauthn を読み込めないため、パスキーは無効になります: {exc}")


class RegisterVerify(BaseModel):
    credential: dict
    name: str | None = None


class LoginVerify(BaseModel):
    credential: dict
    remember: bool = True


class RenameRequest(BaseModel):
    name: str


class DeleteRequest(BaseModel):
    password: str | None = None
    email_code: str | None = None


def _require_available() -> None:
    if not AVAILABLE:
        raise HTTPException(status_code=503, detail="この環境ではパスキーを利用できません。")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _store_challenge(session: Session, challenge: bytes, purpose: str, user_id: int | None) -> None:
    cutoff = datetime.utcnow()
    for old in session.exec(select(WebAuthnChallenge).where(WebAuthnChallenge.expires_at < cutoff)).all():
        session.delete(old)
    session.add(WebAuthnChallenge(
        challenge=_b64(challenge),
        user_id=user_id,
        purpose=purpose,
        expires_at=cutoff + timedelta(minutes=CHALLENGE_MINUTES),
    ))
    session.commit()


def _take_challenge(session: Session, purpose: str, user_id: int | None) -> bytes:
    query = select(WebAuthnChallenge).where(WebAuthnChallenge.purpose == purpose)
    if user_id is not None:
        query = query.where(WebAuthnChallenge.user_id == user_id)
    rows = session.exec(query.order_by(WebAuthnChallenge.created_at.desc())).all()
    for row in rows:
        if security.not_expired(row.expires_at):
            value = _unb64(row.challenge)
            session.delete(row)
            session.commit()
            return value
    raise HTTPException(status_code=400, detail="時間が経過しました。もう一度お試しください。")


def _device_name(raw: str | None, request: Request) -> str:
    if raw and raw.strip():
        return raw.strip()[:NAME_MAX]
    agent = (request.headers.get("user-agent") or "").lower()
    if "iphone" in agent:
        return "iPhone"
    if "ipad" in agent:
        return "iPad"
    if "android" in agent:
        return "Android"
    if "mac" in agent:
        return "Mac"
    if "windows" in agent:
        return "Windows"
    return "パスキー"


def _payload(key: Passkey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
    }


@router.get("/status")
def status(user: User = Depends(current_user), session: Session = Depends(get_session)):
    keys = session.exec(select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.created_at)).all()
    return {"available": AVAILABLE, "passkeys": [_payload(k) for k in keys]}


@router.get("/config")
def passkey_config():
    """ログイン画面が、パスキーのボタンを出してよいか判断するために使う。"""
    return {"available": AVAILABLE}


@router.post("/register/options")
def register_options(user: User = Depends(current_user), session: Session = Depends(get_session)):
    _require_available()
    existing = session.exec(select(Passkey).where(Passkey.user_id == user.id)).all()
    options = _webauthn.generate_registration_options(
        rp_id=config.PASSKEY_RP_ID,
        rp_name=config.PASSKEY_RP_NAME,
        user_id=str(user.id).encode("utf-8"),
        user_name=user.username,
        user_display_name=user.display_name or user.username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=_unb64(k.credential_id)) for k in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _store_challenge(session, options.challenge, "register", user.id)
    return json.loads(options_to_json(options))


@router.post("/register/verify")
def register_verify(
    payload: RegisterVerify,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    _require_available()
    challenge = _take_challenge(session, "register", user.id)
    try:
        result = _webauthn.verify_registration_response(
            credential=payload.credential,
            expected_challenge=challenge,
            expected_rp_id=config.PASSKEY_RP_ID,
            expected_origin=config.PASSKEY_ORIGIN,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"パスキーを登録できませんでした: {exc}")

    credential_id = _b64(result.credential_id)
    if session.exec(select(Passkey).where(Passkey.credential_id == credential_id)).first():
        raise HTTPException(status_code=409, detail="このパスキーはすでに登録されています。")

    key = Passkey(
        user_id=user.id,
        credential_id=credential_id,
        public_key=_b64(result.credential_public_key),
        sign_count=result.sign_count or 0,
        transports=",".join(payload.credential.get("response", {}).get("transports", []) or []),
        name=_device_name(payload.name, request),
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return {"registered": True, "passkey": _payload(key)}


@router.post("/login/options")
def login_options(session: Session = Depends(get_session)):
    _require_available()
    options = _webauthn.generate_authentication_options(
        rp_id=config.PASSKEY_RP_ID,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _store_challenge(session, options.challenge, "login", None)
    return json.loads(options_to_json(options))


@router.post("/login/verify")
def login_verify(
    payload: LoginVerify,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    _require_available()
    raw_id = payload.credential.get("id") or payload.credential.get("rawId")
    if not raw_id:
        raise HTTPException(status_code=400, detail="パスキーの情報が不完全です。")
    key = session.exec(select(Passkey).where(Passkey.credential_id == raw_id)).first()
    if not key:
        raise HTTPException(status_code=404, detail="このパスキーは登録されていません。")
    user = session.get(User, key.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=403, detail="このアカウントは利用できません。")

    challenge = _take_challenge(session, "login", None)
    try:
        result = _webauthn.verify_authentication_response(
            credential=payload.credential,
            expected_challenge=challenge,
            expected_rp_id=config.PASSKEY_RP_ID,
            expected_origin=config.PASSKEY_ORIGIN,
            credential_public_key=_unb64(key.public_key),
            credential_current_sign_count=key.sign_count,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"パスキーを確認できませんでした: {exc}")

    key.sign_count = result.new_sign_count
    key.last_used_at = datetime.utcnow()
    session.add(key)

    ip_hash, ua_hash = security.request_fingerprint(request)
    _remember_device(session, user.id, ip_hash, ua_hash)

    token = security.new_session_token()
    expires_at = datetime.utcnow() + timedelta(days=30 if payload.remember else 1)
    session.add(SessionModel(token=token, user_id=user.id, expires_at=expires_at, remember=payload.remember))
    session.commit()
    _set_session_cookie(response, token, payload.remember)
    return {"authenticated": True, "token": token, "user": public_user(user, user)}


@router.patch("/{passkey_id}")
def rename_passkey(
    passkey_id: int,
    payload: RenameRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    key = session.get(Passkey, passkey_id)
    if not key or key.user_id != user.id:
        raise HTTPException(status_code=404, detail="そのパスキーは見つかりません。")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="名前を入力してください。")
    key.name = name[:NAME_MAX]
    session.add(key)
    session.commit()
    return {"passkey": _payload(key)}


@router.post("/{passkey_id}/delete")
def delete_passkey(
    passkey_id: int,
    payload: DeleteRequest,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    key = session.get(Passkey, passkey_id)
    if not key or key.user_id != user.id:
        raise HTTPException(status_code=404, detail="そのパスキーは見つかりません。")
    verify_identity(session, user, "passkey_delete", payload.password, payload.email_code)
    session.delete(key)
    session.commit()
    return {"deleted": True}
