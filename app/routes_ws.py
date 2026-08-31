"""Me Talk WebSocket エンドポイント。

クライアントは /ws?token=<セッショントークン> で接続する。
接続後はサーバーからのイベント(message.openchat / message.dm / post.new など)を受信する。
クライアントからの ping には pong を返す(接続維持用)。

通話の接続情報(call.signal)はここで中継する。
サーバーは中身を解釈せず、同じ通話にいる相手へそのまま渡すだけ。
音声や映像はサーバーを通らず端末どうしで直接やり取りされる。

Dev yuzuki_akrdev.ofc
"""

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.realtime import authenticate_ws_token, extract_ws_token, manager

router = APIRouter()


async def _relay_signal(sender_id: int, data: dict) -> None:
    """通話の接続情報を、同じ通話にいる相手にだけ渡す。

    宛先が本当に同じ通話の参加者かをサーバー側で確かめる。
    これを省くと、通話に入っていない相手へ勝手に送りつけられる。
    """
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import CallParticipant, CallSession

    call_id = data.get("call_id")
    target_id = data.get("to")
    if not call_id or not target_id:
        return
    with Session(engine) as session:
        call = session.get(CallSession, call_id)
        if not call or call.status != "active":
            return
        rows = session.exec(
            select(CallParticipant).where(
                CallParticipant.call_id == call_id,
                CallParticipant.left_at == None,  # noqa: E711
            )
        ).all()
        members = {r.user_id for r in rows}
    if sender_id not in members or target_id not in members:
        return
    await manager.send_to_users([target_id], {
        "type": "call.signal",
        "data": {
            "call_id": call_id,
            "from": sender_id,
            "kind": data.get("kind"),
            "payload": data.get("payload"),
        },
    })


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    token = extract_ws_token(websocket)
    user = authenticate_ws_token(token)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await manager.connect(user.id, websocket)
    try:
        await websocket.send_text(json.dumps({"type": "ready", "data": {"user_id": user.id}}))
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = message.get("type")
            if kind == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif kind == "call.signal":
                await _relay_signal(user.id, message.get("data") or {})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect(user.id, websocket)
