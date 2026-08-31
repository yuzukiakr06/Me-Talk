"""Me Talk リアルタイム接続管理。

WebSocket 接続を user_id ごとに束ね、オープンチャット/DM/グローバル投稿の
イベントを対象ユーザーへ配信する。MVPはインメモリ管理。将来プロセスを
複数に分ける際は Redis pub/sub をこの層に差し込む。

Dev yuzuki_akrdev.ofc
"""

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from app import config, security
from app.db import engine
from app.models import Session as SessionModel, User
from sqlmodel import Session


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.setdefault(user_id, set()).add(websocket)

    async def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(user_id)
            if conns:
                conns.discard(websocket)
                if not conns:
                    self._connections.pop(user_id, None)

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    async def send_to_users(self, user_ids: list[int], event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        targets: list[WebSocket] = []
        async with self._lock:
            seen = set()
            for uid in user_ids:
                if uid in seen:
                    continue
                seen.add(uid)
                for ws in self._connections.get(uid, set()):
                    targets.append(ws)
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def broadcast_all(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        async with self._lock:
            targets = [ws for conns in self._connections.values() for ws in conns]
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                pass


manager = ConnectionManager()


def authenticate_ws_token(token: str | None) -> User | None:
    """WebSocket接続時のトークン認証。クエリ/サブプロトコルで渡されたトークンを検証する。"""
    if not token:
        return None
    with Session(engine) as session:
        record = session.get(SessionModel, token)
        if not record or not security.not_expired(record.expires_at):
            return None
        user = session.get(User, record.user_id)
        if not user or not user.is_active:
            return None
        return user


def extract_ws_token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token")
    if token:
        return token
    auth = websocket.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return websocket.cookies.get(config.SESSION_COOKIE_NAME)
