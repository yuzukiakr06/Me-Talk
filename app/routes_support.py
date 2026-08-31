"""Me Talk サポート・問い合わせルーター。

ユーザーからのバグ報告と問い合わせを受け取り、
データベースに保存したうえで運営宛にメールで通知する。
開発者バッジを持つユーザーは受信箱から一覧・対応状況の更新ができる。

Dev yuzuki_akrdev.ofc
"""

from datetime import datetime, timedelta
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config
from app.db import get_session
from app.email_util import send_mail
from app.models import SupportTicket, User
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/support", tags=["support"])

KINDS = {
    "bug": "バグ報告",
    "question": "使い方の質問",
    "request": "機能の要望",
    "other": "その他",
}

STATUSES = {
    "open": "未対応",
    "in_progress": "対応中",
    "resolved": "対応済み",
    "closed": "クローズ",
}

SUBJECT_MAX = 100
BODY_MAX = 4000
RATE_LIMIT_PER_HOUR = 5


class TicketCreate(BaseModel):
    kind: str = "bug"
    subject: str
    body: str
    contact_email: str | None = None
    client_info: str | None = None


class TicketUpdate(BaseModel):
    status: str | None = None
    admin_note: str | None = None


def require_developer(user: User = Depends(current_user)) -> User:
    if "developer" not in (user.badges or "").split(","):
        raise HTTPException(status_code=403, detail="この操作は開発者のみ行えます。")
    return user


def _ticket_payload(ticket: SupportTicket, author: User | None = None) -> dict:
    data = {
        "id": ticket.id,
        "kind": ticket.kind,
        "kind_label": KINDS.get(ticket.kind, ticket.kind),
        "subject": ticket.subject,
        "body": ticket.body,
        "contact_email": ticket.contact_email,
        "status": ticket.status,
        "status_label": STATUSES.get(ticket.status, ticket.status),
        "admin_note": ticket.admin_note,
        "app_version": ticket.app_version,
        "client_info": ticket.client_info,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
    }
    if author is not None:
        data["author"] = public_user(author)
    return data


def _notify(ticket: SupportTicket, author: User) -> bool:
    """運営宛に新着の報告をメールで知らせる。"""
    to = config.SUPPORT_EMAIL
    if not to:
        return False
    kind_label = KINDS.get(ticket.kind, ticket.kind)
    subject = f"[{config.SITE_NAME}] {kind_label} #{ticket.id} {ticket.subject}"
    lines = [
        f"{config.SITE_NAME} に新しい{kind_label}が届きました。",
        "",
        f"受付番号: #{ticket.id}",
        f"種類: {kind_label}",
        f"件名: {ticket.subject}",
        f"送信者: {author.display_name or author.username} (@{author.username})",
        f"ユーザーID: {author.user_id or ''}",
        f"連絡先: {ticket.contact_email or author.email}",
        f"バージョン: {ticket.app_version}",
        f"環境: {ticket.client_info}",
        f"受付日時: {ticket.created_at.strftime('%Y-%m-%d %H:%M:%S')} (UTC)",
        "",
        "----- 本文 -----",
        ticket.body,
        "----------------",
        "",
        f"{config.BASE_URL.rstrip('/')}/app",
    ]
    text = "\n".join(lines)
    rows = [
        ("受付番号", f"#{ticket.id}"),
        ("種類", kind_label),
        ("件名", ticket.subject),
        ("送信者", f"{author.display_name or author.username} (@{author.username})"),
        ("ユーザーID", author.user_id or ""),
        ("連絡先", ticket.contact_email or author.email),
        ("バージョン", ticket.app_version),
        ("環境", ticket.client_info),
    ]
    table = "".join(
        '<tr><td style="padding:6px 12px 6px 0;color:#8b93a1;font-size:13px;white-space:nowrap">'
        f'{escape(label)}</td>'
        '<td style="padding:6px 0;color:#e6ebf2;font-size:13px">'
        f'{escape(str(value))}</td></tr>'
        for label, value in rows
    )
    html = (
        '<div style="background:#0b0f14;padding:24px;font-family:sans-serif">'
        '<div style="max-width:640px;margin:0 auto;background:#141b24;border-radius:14px;padding:24px">'
        f'<div style="color:#34d399;font-size:18px;font-weight:bold;margin-bottom:4px">{escape(config.SITE_NAME)} サポート</div>'
        f'<div style="color:#8b93a1;font-size:13px;margin-bottom:18px">新しい{escape(kind_label)}が届きました。</div>'
        f'<table style="border-collapse:collapse;width:100%">{table}</table>'
        '<div style="height:1px;background:#232c3a;margin:18px 0"></div>'
        '<div style="color:#8b93a1;font-size:12px;margin-bottom:6px">本文</div>'
        '<div style="color:#e6ebf2;font-size:14px;line-height:1.7;white-space:pre-wrap">'
        f'{escape(ticket.body)}</div>'
        '</div></div>'
    )
    return send_mail(to, subject, text, html)


@router.post("/tickets")
def create_ticket(
    payload: TicketCreate,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    kind = payload.kind if payload.kind in KINDS else "other"
    subject = (payload.subject or "").strip()
    body = (payload.body or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="件名を入力してください。")
    if len(subject) > SUBJECT_MAX:
        raise HTTPException(status_code=400, detail=f"件名は{SUBJECT_MAX}文字以内にしてください。")
    if not body:
        raise HTTPException(status_code=400, detail="内容を入力してください。")
    if len(body) > BODY_MAX:
        raise HTTPException(status_code=400, detail=f"内容は{BODY_MAX}文字以内にしてください。")

    since = datetime.utcnow() - timedelta(hours=1)
    recent = session.exec(
        select(SupportTicket).where(SupportTicket.user_id == user.id, SupportTicket.created_at >= since)
    ).all()
    if len(recent) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(status_code=429, detail="短時間に送信できる件数の上限に達しました。時間をおいてお試しください。")

    ticket = SupportTicket(
        user_id=user.id,
        kind=kind,
        subject=subject,
        body=body,
        contact_email=(payload.contact_email or "").strip()[:200],
        app_version=config.APP_VERSION,
        client_info=(payload.client_info or request.headers.get("user-agent") or "")[:300],
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)

    notified = False
    try:
        notified = _notify(ticket, user)
    except Exception as exc:
        print(f"[support] 通知メールの送信に失敗しました: {exc}")

    data = _ticket_payload(ticket)
    data["notified"] = notified
    return data


@router.get("/kinds")
def list_kinds():
    return {
        "kinds": [{"key": k, "label": v} for k, v in KINDS.items()],
        "statuses": [{"key": k, "label": v} for k, v in STATUSES.items()],
    }


@router.get("/tickets")
def my_tickets(user: User = Depends(current_user), session: Session = Depends(get_session)):
    tickets = session.exec(
        select(SupportTicket).where(SupportTicket.user_id == user.id).order_by(SupportTicket.created_at.desc())
    ).all()
    return {"tickets": [_ticket_payload(t) for t in tickets]}


@router.get("/admin/tickets")
def admin_tickets(
    status: str = "",
    q: str = "",
    admin: User = Depends(require_developer),
    session: Session = Depends(get_session),
):
    query = select(SupportTicket)
    if status and status in STATUSES:
        query = query.where(SupportTicket.status == status)
    tickets = session.exec(query.order_by(SupportTicket.created_at.desc())).all()
    keyword = (q or "").strip().lower()
    result = []
    for ticket in tickets:
        author = session.get(User, ticket.user_id)
        if keyword:
            haystack = " ".join([
                ticket.subject or "",
                ticket.body or "",
                (author.username if author else ""),
                (author.display_name if author else ""),
            ]).lower()
            if keyword not in haystack:
                continue
        result.append(_ticket_payload(ticket, author))
    counts = {key: 0 for key in STATUSES}
    for ticket in session.exec(select(SupportTicket)).all():
        if ticket.status in counts:
            counts[ticket.status] += 1
    return {"tickets": result, "counts": counts}


@router.patch("/admin/tickets/{ticket_id}")
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    admin: User = Depends(require_developer),
    session: Session = Depends(get_session),
):
    ticket = session.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="その報告は見つかりません。")
    if payload.status is not None:
        if payload.status not in STATUSES:
            raise HTTPException(status_code=400, detail="対応状況の値が不正です。")
        ticket.status = payload.status
    if payload.admin_note is not None:
        ticket.admin_note = payload.admin_note.strip()[:2000]
    ticket.updated_at = datetime.utcnow()
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return _ticket_payload(ticket, session.get(User, ticket.user_id))
