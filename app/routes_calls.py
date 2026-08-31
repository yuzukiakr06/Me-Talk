"""Me Talk 通話。

音声・ビデオ通話はブラウザ同士を直接つなぐ WebRTC で行う。
サーバーは相手を呼び出し、接続に必要な情報を仲介するだけで、
音声や映像そのものはサーバーを通らない。

参加者どうしが総当たりでつながる方式(メッシュ)のため、
人数が増えるほど各端末の負担が増える。
CALL_MAX_PARTICIPANTS で上限を設けている。

Dev yuzuki_akrdev.ofc
"""

import time
from datetime import datetime, timedelta

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import config, sfu, social
from app.db import get_session
from app.models import (
    CallParticipant,
    CallSession,
    DMConversation,
    OpenChat,
    OpenChatMember,
    User,
)
from app.realtime import manager
from app.routes_auth import current_user, public_user

router = APIRouter(prefix="/api/calls", tags=["calls"])

STALE_MINUTES = 6 * 60


class StartCall(BaseModel):
    scope: str
    target_id: int
    video: bool = False
    listen_only: bool = False


class JoinCall(BaseModel):
    listen_only: bool = False
    video: bool = False


class CallState(BaseModel):
    muted: bool | None = None
    deafened: bool | None = None
    video_on: bool | None = None
    screen_on: bool | None = None
    listen_only: bool | None = None


_turn_cache = {"servers": None, "expires": 0}

CLOUDFLARE_TURN_API = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"


def _cloudflare_turn() -> list:
    """Cloudflare から期限つきのTURN認証情報を取り出す。

    Cloudflare は固定のユーザー名とパスワードを発行しない。
    サーバー側で鍵を使って短命の認証情報を作り、それを端末へ渡す方式になっている。
    毎回問い合わせると遅くなるので、期限の手前まで使い回す。
    """
    if not config.TURN_KEY_ID or not config.TURN_KEY_API_TOKEN:
        return []
    now = time.time()
    if _turn_cache["servers"] and now < _turn_cache["expires"]:
        return _turn_cache["servers"]
    try:
        res = requests.post(
            CLOUDFLARE_TURN_API.format(key_id=config.TURN_KEY_ID),
            headers={
                "Authorization": f"Bearer {config.TURN_KEY_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"ttl": config.TURN_TTL_SECONDS},
            timeout=8,
        )
        if res.status_code not in (200, 201):
            print(f"[call] TURN認証情報を取得できませんでした: {res.status_code} {res.text[:180]}")
            return []
        servers = res.json().get("iceServers") or []
        if isinstance(servers, dict):
            servers = [servers]
        _turn_cache["servers"] = servers
        _turn_cache["expires"] = now + max(60, config.TURN_TTL_SECONDS - 600)
        return servers
    except Exception as exc:
        print(f"[call] TURN認証情報の取得に失敗しました: {exc}")
        return []


def _ice_servers() -> list:
    """接続に使うサーバー一覧を組み立てる。

    Cloudflare の鍵があればそれを最優先で使い、
    無ければ手動設定のTURN、それも無ければSTUNだけを返す。
    """
    cloudflare = _cloudflare_turn()
    if cloudflare:
        return cloudflare

    servers = []
    stun = [u.strip() for u in (config.STUN_URLS or "").split(",") if u.strip()]
    if stun:
        servers.append({"urls": stun})
    turn = [u.strip() for u in (config.TURN_URLS or "").split(",") if u.strip()]
    if turn and config.TURN_USERNAME and config.TURN_PASSWORD:
        servers.append({
            "urls": turn,
            "username": config.TURN_USERNAME,
            "credential": config.TURN_PASSWORD,
        })
    return servers


def _check_access(session: Session, user: User, scope: str, target_id: int) -> None:
    """その相手や場所で通話してよいか確かめる。"""
    if scope == "dm":
        conv = session.get(DMConversation, target_id)
        if not conv or user.id not in (conv.user_a, conv.user_b):
            raise HTTPException(status_code=404, detail="この会話は見つかりません。")
        other_id = conv.user_b if conv.user_a == user.id else conv.user_a
        other = session.get(User, other_id)
        ok, reason = social.can_call(session, user, other)
        if not ok:
            raise HTTPException(status_code=403, detail=reason)
    elif scope == "openchat":
        oc = session.get(OpenChat, target_id)
        if not oc:
            raise HTTPException(status_code=404, detail="グループが見つかりません。")
        member = session.exec(
            select(OpenChatMember).where(
                OpenChatMember.openchat_id == target_id,
                OpenChatMember.user_id == user.id,
            )
        ).first()
        if not member:
            raise HTTPException(status_code=403, detail="このグループに参加していません。")
    else:
        raise HTTPException(status_code=400, detail="通話の種類が不正です。")


def _call_mode(scope: str) -> str:
    """通話の方式を決める。

    DMは必ず2人なので、直接つないだ方が遅延が小さく転送量もかからない。
    グループは人数が読めないため、中継(SFU)を使って各自1本の接続にまとめる。
    """
    if scope == "openchat" and sfu.enabled():
        return "sfu"
    return "mesh"


def _capacity(call: CallSession) -> int:
    if call.mode == "sfu":
        return config.SFU_MAX_PARTICIPANTS
    return config.CALL_MAX_PARTICIPANTS


def _active_participants(session: Session, call_id: int) -> list:
    return session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call_id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).all()


def _call_payload(session: Session, call: CallSession) -> dict:
    rows = _active_participants(session, call.id)
    people = []
    for row in rows:
        person = session.get(User, row.user_id)
        if not person:
            continue
        people.append({
            "user": public_user(person),
            "sfu_session_id": row.sfu_session_id,
            "audio_track": row.audio_track,
            "video_track": row.video_track,
            "muted": row.muted,
            "deafened": row.deafened,
            "video_on": row.video_on,
            "screen_on": row.screen_on,
            "listen_only": row.listen_only,
            "online": manager.is_online(row.user_id),
        })
    return {
        "id": call.id,
        "mode": call.mode,
        "scope": call.scope,
        "target_id": call.target_id,
        "status": call.status,
        "video": call.video,
        "started_by": call.started_by,
        "created_at": call.created_at.isoformat(),
        "participants": people,
        "max_participants": _capacity(call),
    }


def _find_open_call(session: Session, scope: str, target_id: int) -> CallSession | None:
    call = session.exec(
        select(CallSession).where(
            CallSession.scope == scope,
            CallSession.target_id == target_id,
            CallSession.status == "active",
        ).order_by(CallSession.id.desc())
    ).first()
    if not call:
        return None
    stale = datetime.utcnow() - timedelta(minutes=STALE_MINUTES)
    if call.created_at < stale and not _active_participants(session, call.id):
        call.status = "ended"
        call.ended_at = datetime.utcnow()
        session.add(call)
        session.commit()
        return None
    return call


async def _notify_room(session: Session, call: CallSession, event: str) -> None:
    """通話の状態が変わったことを関係者へ知らせる。"""
    if call.scope == "dm":
        conv = session.get(DMConversation, call.target_id)
        ids = [conv.user_a, conv.user_b] if conv else []
    else:
        ids = [m.user_id for m in session.exec(
            select(OpenChatMember).where(OpenChatMember.openchat_id == call.target_id)
        ).all()]
    await manager.send_to_users(ids, {"type": event, "data": _call_payload(session, call)})


@router.get("/config")
def call_config(user: User = Depends(current_user)):
    """接続に必要なサーバー情報と制限を返す。"""
    servers = _ice_servers()
    has_turn = any(
        "username" in s and any("turn:" in u or "turns:" in u for u in
                                (s["urls"] if isinstance(s.get("urls"), list) else [s.get("urls", "")]))
        for s in servers
    )
    return {
        "ice_servers": servers,
        "has_turn": has_turn,
        "sfu_enabled": sfu.enabled(),
        "sfu_max_participants": config.SFU_MAX_PARTICIPANTS,
        "provider": "cloudflare" if config.TURN_KEY_ID and has_turn else ("manual" if has_turn else "none"),
        "max_participants": config.CALL_MAX_PARTICIPANTS,
    }


@router.get("/active")
def active_call(
    scope: str = "",
    target_id: int = 0,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """指定した場所で進行中の通話、または自分が参加中の通話を返す。"""
    if scope and target_id:
        call = _find_open_call(session, scope, target_id)
        return {"call": _call_payload(session, call) if call else None}
    rows = session.exec(
        select(CallParticipant).where(
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).all()
    for row in rows:
        call = session.get(CallSession, row.call_id)
        if call and call.status == "active":
            return {"call": _call_payload(session, call)}
    return {"call": None}


@router.post("/start")
async def start_call(
    payload: StartCall,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """通話を始める。すでに進行中なら、その通話に参加する。"""
    _check_access(session, user, payload.scope, payload.target_id)
    call = _find_open_call(session, payload.scope, payload.target_id)
    created = False
    if not call:
        call = CallSession(
            mode=_call_mode(payload.scope),
            scope=payload.scope,
            target_id=payload.target_id,
            started_by=user.id,
            video=payload.video,
        )
        session.add(call)
        session.commit()
        session.refresh(call)
        created = True

    existing = session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).first()
    if not existing:
        limit = _capacity(call)
        if len(_active_participants(session, call.id)) >= limit:
            raise HTTPException(status_code=409, detail=f"この通話は満員です(最大{limit}人)。")
        session.add(CallParticipant(
            call_id=call.id,
            user_id=user.id,
            video_on=payload.video and not payload.listen_only,
            listen_only=payload.listen_only,
            muted=payload.listen_only,
        ))
        session.commit()

    await _notify_room(session, call, "call.started" if created else "call.updated")
    return _call_payload(session, call)


@router.post("/{call_id}/join")
async def join_call(
    call_id: int,
    payload: JoinCall,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    call = session.get(CallSession, call_id)
    if not call or call.status != "active":
        raise HTTPException(status_code=404, detail="この通話はすでに終了しています。")
    _check_access(session, user, call.scope, call.target_id)

    existing = session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).first()
    if not existing:
        limit = _capacity(call)
        if len(_active_participants(session, call.id)) >= limit:
            raise HTTPException(status_code=409, detail=f"この通話は満員です(最大{limit}人)。")
        session.add(CallParticipant(
            call_id=call.id,
            user_id=user.id,
            video_on=payload.video and not payload.listen_only,
            listen_only=payload.listen_only,
            muted=payload.listen_only,
        ))
        session.commit()
    await _notify_room(session, call, "call.updated")
    return _call_payload(session, call)


@router.post("/{call_id}/leave")
async def leave_call(
    call_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    call = session.get(CallSession, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="この通話は見つかりません。")
    row = session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).first()
    if row:
        if call.mode == "sfu" and row.sfu_session_id:
            names = [n for n in (row.audio_track, row.video_track) if n]
            if names:
                try:
                    sfu.close_tracks(row.sfu_session_id, names, force=True)
                except sfu.SfuError as exc:
                    print(f"[call] トラックの終了に失敗しました: {exc}")
        row.left_at = datetime.utcnow()
        session.add(row)
        session.commit()
    if not _active_participants(session, call.id):
        call.status = "ended"
        call.ended_at = datetime.utcnow()
        session.add(call)
        session.commit()
        await _notify_room(session, call, "call.ended")
    else:
        await _notify_room(session, call, "call.updated")
    return {"left": True, "ended": call.status == "ended"}


class SfuSession(BaseModel):
    sdp: str


class SfuPublish(BaseModel):
    sdp: str
    tracks: list


class SfuSubscribe(BaseModel):
    tracks: list


class SfuAnswer(BaseModel):
    sdp: str


def _require_member(session: Session, call_id: int, user: User) -> tuple:
    """通話の参加者であることを確かめてから中継を許す。

    これを省くと、通話に入っていない人が他人のトラックを引き込めてしまう。
    """
    call = session.get(CallSession, call_id)
    if not call or call.status != "active":
        raise HTTPException(status_code=404, detail="この通話はすでに終了しています。")
    if call.mode != "sfu":
        raise HTTPException(status_code=400, detail="この通話は中継方式ではありません。")
    row = session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).first()
    if not row:
        raise HTTPException(status_code=403, detail="この通話に参加していません。")
    return call, row


@router.post("/{call_id}/sfu/session")
def sfu_session(
    call_id: int,
    payload: SfuSession,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """中継サーバーとの接続を開く。申し出を渡して応答を受け取る。"""
    call, row = _require_member(session, call_id, user)
    try:
        created = sfu.new_session(payload.sdp)
    except sfu.SfuError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    session_id = created.get("sessionId", "")
    if not session_id:
        raise HTTPException(status_code=502, detail="セッションを作成できませんでした。")
    row.sfu_session_id = session_id
    session.add(row)
    session.commit()
    return {"session_id": session_id, "answer": created.get("sessionDescription")}


@router.post("/{call_id}/sfu/publish")
async def sfu_publish(
    call_id: int,
    payload: SfuPublish,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """自分の音声と映像の送出を確定させる。"""
    call, row = _require_member(session, call_id, user)
    if not row.sfu_session_id:
        raise HTTPException(status_code=400, detail="先に接続を開いてください。")
    try:
        result = sfu.publish_tracks(row.sfu_session_id, payload.sdp, payload.tracks)
    except sfu.SfuError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    for track in result.get("tracks", []) or []:
        name = track.get("trackName", "")
        mid = str(track.get("mid", ""))
        if not name:
            continue
        if mid == "0" or "audio" in name.lower():
            row.audio_track = name
        else:
            row.video_track = name
    session.add(row)
    session.commit()

    await _notify_room(session, call, "call.updated")
    return {
        "session_id": row.sfu_session_id,
        "answer": result.get("sessionDescription"),
        "tracks": result.get("tracks", []),
        "requires_renegotiation": result.get("requiresImmediateRenegotiation", False),
    }


@router.post("/{call_id}/sfu/subscribe")
def sfu_subscribe(
    call_id: int,
    payload: SfuSubscribe,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """他の参加者のトラックを引き込む。指定できるのは同じ通話の参加者のものだけ。"""
    call, row = _require_member(session, call_id, user)
    if not row.sfu_session_id:
        raise HTTPException(status_code=400, detail="先に自分の接続を確立してください。")

    allowed = {}
    for other in _active_participants(session, call.id):
        if other.sfu_session_id:
            allowed.setdefault(other.sfu_session_id, set())
            if other.audio_track:
                allowed[other.sfu_session_id].add(other.audio_track)
            if other.video_track:
                allowed[other.sfu_session_id].add(other.video_track)

    wanted = []
    for track in payload.tracks or []:
        sid = str(track.get("sessionId", ""))
        name = str(track.get("trackName", ""))
        if sid in allowed and name in allowed[sid]:
            wanted.append({"location": "remote", "sessionId": sid, "trackName": name})
    if not wanted:
        raise HTTPException(status_code=400, detail="引き込めるトラックがありません。")

    try:
        result = sfu.subscribe_tracks(row.sfu_session_id, wanted)
    except sfu.SfuError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "offer": result.get("sessionDescription"),
        "tracks": result.get("tracks", []),
        "requires_renegotiation": result.get("requiresImmediateRenegotiation", False),
    }


@router.put("/{call_id}/sfu/renegotiate")
def sfu_renegotiate(
    call_id: int,
    payload: SfuAnswer,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """中継サーバーからの申し出に返事をする。"""
    call, row = _require_member(session, call_id, user)
    if not row.sfu_session_id:
        raise HTTPException(status_code=400, detail="接続が確立していません。")
    try:
        sfu.renegotiate(row.sfu_session_id, payload.sdp)
    except sfu.SfuError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@router.patch("/{call_id}/state")
async def update_state(
    call_id: int,
    payload: CallState,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """マイクや画面共有の状態を他の参加者へ知らせる。"""
    call = session.get(CallSession, call_id)
    if not call or call.status != "active":
        raise HTTPException(status_code=404, detail="この通話はすでに終了しています。")
    row = session.exec(
        select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.user_id == user.id,
            CallParticipant.left_at == None,  # noqa: E711
        )
    ).first()
    if not row:
        raise HTTPException(status_code=403, detail="この通話に参加していません。")
    for field in ("muted", "deafened", "video_on", "screen_on", "listen_only"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, bool(value))
    if row.listen_only:
        row.video_on = False
        row.screen_on = False
        row.muted = True
    session.add(row)
    session.commit()
    await _notify_room(session, call, "call.updated")
    return _call_payload(session, call)
