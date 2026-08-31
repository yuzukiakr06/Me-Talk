"""Me Talk 2段階認証ルーター。

認証アプリ(TOTP)による2段階認証の有効化・解除と、
バックアップコードの発行・確認を担当する。

バックアップコードの再発行は開発者のみが自分で行える。
一般ユーザーはサポートからの申請にもとづき、開発者が手動で発行する。

Dev yuzuki_akrdev.ofc
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config, totp
from app.db import get_session
from app.models import BackupCode, User
from app.routes_auth import current_user, has_password, verify_identity

router = APIRouter(prefix="/api/auth/2fa", tags=["2fa"])


class StartRequest(BaseModel):
    password: str | None = None
    email_code: str | None = None


class EnableRequest(BaseModel):
    totp_code: str


class DisableRequest(BaseModel):
    totp_code: str | None = None
    backup_code: str | None = None
    password: str | None = None
    email_code: str | None = None


class RegenerateRequest(BaseModel):
    totp_code: str | None = None
    password: str | None = None
    email_code: str | None = None


def is_developer(user: User) -> bool:
    return "developer" in (user.badges or "").split(",")


def remaining_backup_codes(session: Session, user_id: int) -> int:
    codes = session.exec(
        select(BackupCode).where(BackupCode.user_id == user_id, BackupCode.used_at == None)  # noqa: E711
    ).all()
    return len(codes)


def issue_backup_codes(session: Session, user: User) -> list:
    """既存のバックアップコードを破棄して10個作り直す。平文はこの一度だけ返す。"""
    existing = session.exec(select(BackupCode).where(BackupCode.user_id == user.id)).all()
    for row in existing:
        session.delete(row)
    codes = totp.make_backup_codes()
    for code in codes:
        session.add(BackupCode(user_id=user.id, code_hash=totp.backup_hash(code)))
    session.commit()
    return codes


def consume_backup_code(session: Session, user: User, code: str) -> bool:
    """バックアップコードを1つ使う。使えたら True。"""
    digest = totp.backup_hash(code)
    if not totp.normalize_backup_code(code):
        return False
    row = session.exec(
        select(BackupCode).where(
            BackupCode.user_id == user.id,
            BackupCode.code_hash == digest,
            BackupCode.used_at == None,  # noqa: E711
        )
    ).first()
    if not row:
        return False
    row.used_at = datetime.utcnow()
    session.add(row)
    session.commit()
    return True


def check_second_factor(session: Session, user: User, totp_code: str | None, backup_code: str | None) -> None:
    """2段階認証が有効なユーザーに、認証アプリのコードかバックアップコードを求める。"""
    if not user.totp_enabled or not user.totp_secret:
        return
    if totp_code and totp.verify(user.totp_secret, totp_code):
        return
    if backup_code and consume_backup_code(session, user, backup_code):
        return
    if totp_code or backup_code:
        raise HTTPException(status_code=401, detail="認証コードが正しくありません。")
    raise HTTPException(status_code=401, detail={
        "totp_required": True,
        "message": "認証アプリのコードを入力してください。",
    })


@router.get("/status")
def status(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return {
        "enabled": bool(user.totp_enabled),
        "pending": bool(user.totp_secret and not user.totp_enabled),
        "backup_codes_remaining": remaining_backup_codes(session, user.id) if user.totp_enabled else 0,
        "can_regenerate": is_developer(user),
        "has_password": has_password(user),
        "enabled_at": user.totp_enabled_at.isoformat() if user.totp_enabled_at else None,
    }


@router.post("/start")
def start(payload: StartRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """共有鍵を作り、認証アプリに読み込ませるQRコードを返す。この時点ではまだ有効にしない。"""
    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="2段階認証はすでに有効です。")
    verify_identity(session, user, "totp_setup", payload.password, payload.email_code)
    secret = totp.make_secret()
    user.totp_secret = secret
    session.add(user)
    session.commit()
    uri = totp.provisioning_uri(secret, user.email, config.SITE_NAME)
    return {
        "secret": secret,
        "secret_formatted": totp.format_secret(secret),
        "uri": uri,
        "qr_svg": totp.qr_svg(uri),
    }


@router.post("/enable")
def enable(payload: EnableRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """認証アプリのコードを確認して2段階認証を有効にし、バックアップコードを発行する。"""
    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="2段階認証はすでに有効です。")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="先に設定を開始してください。")
    if not totp.verify(user.totp_secret, payload.totp_code):
        raise HTTPException(status_code=401, detail="認証コードが正しくありません。認証アプリの時刻設定もご確認ください。")
    user.totp_enabled = True
    user.totp_enabled_at = datetime.utcnow()
    session.add(user)
    session.commit()
    codes = issue_backup_codes(session, user)
    return {"enabled": True, "backup_codes": codes}


@router.post("/disable")
def disable(payload: DisableRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """2段階認証を解除する。本人確認と認証コードの両方を求める。"""
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="2段階認証は有効になっていません。")
    verify_identity(session, user, "totp_disable", payload.password, payload.email_code)
    check_second_factor(session, user, payload.totp_code, payload.backup_code)
    for row in session.exec(select(BackupCode).where(BackupCode.user_id == user.id)).all():
        session.delete(row)
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_enabled_at = None
    session.add(user)
    session.commit()
    return {"enabled": False}


@router.post("/backup-codes")
def regenerate(payload: RegenerateRequest, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """バックアップコードを作り直す。開発者のみが自分で実行できる。"""
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="2段階認証は有効になっていません。")
    if not is_developer(user):
        raise HTTPException(status_code=403, detail={
            "support_required": True,
            "message": "バックアップコードの再発行は、設定のサポートからお申し込みください。本人確認のうえ運営が対応します。",
        })
    verify_identity(session, user, "totp_backup", payload.password, payload.email_code)
    check_second_factor(session, user, payload.totp_code, None)
    codes = issue_backup_codes(session, user)
    return {"backup_codes": codes}
