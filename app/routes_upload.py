"""Me Talk アップロード / プリセット。

画像(アイコン・カバー・トーク背景)のアップロードを受け付け、保存してURLを返す。
プリセット背景も提供する(サーバー内蔵のグラデーション画像URL一覧)。
MVPはローカルディスク保存。将来S3互換に差し替え可能なように保存処理を分離している。

Dev yuzuki_akrdev.ofc
"""

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlmodel import Session

from app.db import get_session
from app.models import Upload, User
from app.routes_auth import current_user

router = APIRouter(prefix="/api", tags=["uploads"])

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_SIZE = 8 * 1024 * 1024
MAX_FILE_SIZE = 50 * 1024 * 1024

VIDEO_TYPES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

PRESET_BACKGROUNDS = [
    {"id": "aurora", "name": "オーロラ", "url": "/static/bg/aurora.svg"},
    {"id": "night", "name": "ナイト", "url": "/static/bg/night.svg"},
    {"id": "sakura", "name": "サクラ", "url": "/static/bg/sakura.svg"},
    {"id": "ocean", "name": "オーシャン", "url": "/static/bg/ocean.svg"},
    {"id": "mono", "name": "モノトーン", "url": "/static/bg/mono.svg"},
    {"id": "sunset", "name": "サンセット", "url": "/static/bg/sunset.svg"},
]


@router.get("/presets/backgrounds")
def preset_backgrounds():
    return {"backgrounds": PRESET_BACKGROUNDS}


@router.post("/upload")
async def upload_image(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="対応していない画像形式です。png/jpeg/webp/gif に対応しています。")
    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="ファイルサイズが大きすぎます(8MBまで)。")
    ext = ALLOWED_TYPES[file.content_type]
    name = f"{secrets.token_urlsafe(16)}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    record = Upload(
        owner_id=user.id,
        filename=name,
        content_type=file.content_type,
        size=len(data),
    )
    session.add(record)
    session.commit()
    return {"url": f"/uploads/{name}", "size": len(data)}


import re as _re


def _safe_name(original: str) -> str:
    base = _re.sub(r"[^a-zA-Z0-9._-]", "_", original or "file")
    return base[-80:] if len(base) > 80 else base


@router.post("/upload/file")
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="ファイルサイズが大きすぎます(50MBまで)。")
    ctype = file.content_type or "application/octet-stream"
    if ctype in ALLOWED_TYPES:
        kind = "image"
        ext = ALLOWED_TYPES[ctype]
    elif ctype in VIDEO_TYPES:
        kind = "video"
        ext = VIDEO_TYPES[ctype]
    else:
        kind = "file"
        ext = ""
        original_ext = _re.search(r"(\.[a-zA-Z0-9]{1,8})$", file.filename or "")
        if original_ext:
            ext = original_ext.group(1)
    name = f"{secrets.token_urlsafe(16)}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    session.add(Upload(owner_id=user.id, filename=name, content_type=ctype, size=len(data)))
    session.commit()
    return {
        "url": f"/uploads/{name}",
        "kind": kind,
        "name": _safe_name(file.filename or name),
        "content_type": ctype,
        "size": len(data),
    }
