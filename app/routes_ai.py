"""Me Talk AIアシストルーター。

オープンチャット/DMの会話履歴を文脈に、要約・翻訳・返信案・予定抽出を提供する。
Me TalkサーバーがAnthropic API(Claude)を呼ぶ。APIキー未設定時は503を返す。

Dev yuzuki_akrdev.ofc
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import ai_util, config
from app.db import get_session
from app.models import DMConversation, DMMessage, OpenChatMember, OpenChatMessage, User
from app.routes_auth import current_user

router = APIRouter(prefix="/api/ai", tags=["ai"])

MAX_CONTEXT_MESSAGES = 60


class TranslatePayload(BaseModel):
    text: str
    target_lang: str = "日本語"


class ChatPayload(BaseModel):
    message: str


def _require_oc_member(session: Session, openchat_id: int, user_id: int):
    member = session.exec(
        select(OpenChatMember).where(
            OpenChatMember.openchat_id == openchat_id,
            OpenChatMember.user_id == user_id,
        )
    ).first()
    if not member:
        raise HTTPException(status_code=403, detail="このグループに参加していません。")


def _oc_transcript(session: Session, openchat_id: int) -> str:
    messages = session.exec(
        select(OpenChatMessage)
        .where(OpenChatMessage.openchat_id == openchat_id, OpenChatMessage.deleted == False)  # noqa: E712
        .order_by(OpenChatMessage.id.desc())
        .limit(MAX_CONTEXT_MESSAGES)
    ).all()
    messages.reverse()
    lines = []
    for m in messages:
        author = session.get(User, m.author_id)
        name = author.username if author else "?"
        content = m.content or "[添付]"
        lines.append(f"{name}: {content}")
    return "\n".join(lines)


def _require_dm(session: Session, conv_id: int, user_id: int) -> DMConversation:
    conv = session.get(DMConversation, conv_id)
    if not conv or user_id not in (conv.user_a, conv.user_b):
        raise HTTPException(status_code=404, detail="会話が見つかりません。")
    return conv


def _dm_transcript(session: Session, conv_id: int) -> str:
    messages = session.exec(
        select(DMMessage)
        .where(DMMessage.conversation_id == conv_id, DMMessage.deleted == False)  # noqa: E712
        .order_by(DMMessage.id.desc())
        .limit(MAX_CONTEXT_MESSAGES)
    ).all()
    messages.reverse()
    lines = []
    for m in messages:
        author = session.get(User, m.author_id)
        name = author.username if author else "?"
        content = m.content or "[添付]"
        lines.append(f"{name}: {content}")
    return "\n".join(lines)


def _guard():
    if not ai_util.is_configured():
        raise HTTPException(status_code=503, detail="AI機能は現在利用できません(未設定)。")


@router.get("/status")
def ai_status():
    return {"available": ai_util.is_configured(), "assistant_name": config.AI_ASSISTANT_NAME}


@router.post("/openchats/{openchat_id}/summarize")
def summarize_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    transcript = _oc_transcript(session, openchat_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="要約する会話がありません。")
    try:
        return {"summary": ai_util.summarize(transcript)}
    except ai_util.AIUnavailable:
        raise HTTPException(status_code=503, detail="AI機能は未設定です。")
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/openchats/{openchat_id}/suggest")
def suggest_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    transcript = _oc_transcript(session, openchat_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="返信案を作る会話がありません。")
    try:
        return {"suggestions": ai_util.reply_suggestions(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/openchats/{openchat_id}/extract-events")
def extract_openchat_events(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    transcript = _oc_transcript(session, openchat_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="予定を抽出する会話がありません。")
    try:
        return {"events": ai_util.extract_events(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/openchats/{openchat_id}/topics")
def topics_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    transcript = _oc_transcript(session, openchat_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="話題を提案する会話がありません。")
    try:
        return {"topics": ai_util.topic_suggestions(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/openchats/{openchat_id}/mood")
def mood_openchat(openchat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    transcript = _oc_transcript(session, openchat_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="分析する会話がありません。")
    try:
        return {"mood": ai_util.analyze_mood(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/dm/{conv_id}/summarize")
def summarize_dm(conv_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_dm(session, conv_id, user.id)
    transcript = _dm_transcript(session, conv_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="要約する会話がありません。")
    try:
        return {"summary": ai_util.summarize(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/dm/{conv_id}/suggest")
def suggest_dm(conv_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_dm(session, conv_id, user.id)
    transcript = _dm_transcript(session, conv_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="返信案を作る会話がありません。")
    try:
        return {"suggestions": ai_util.reply_suggestions(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/dm/{conv_id}/topics")
def topics_dm(conv_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_dm(session, conv_id, user.id)
    transcript = _dm_transcript(session, conv_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="話題を提案する会話がありません。")
    try:
        return {"topics": ai_util.topic_suggestions(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/dm/{conv_id}/mood")
def mood_dm(conv_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_dm(session, conv_id, user.id)
    transcript = _dm_transcript(session, conv_id)
    if not transcript:
        raise HTTPException(status_code=400, detail="分析する会話がありません。")
    try:
        return {"mood": ai_util.analyze_mood(transcript)}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/translate")
def translate_text(payload: TranslatePayload, user: User = Depends(current_user)):
    _guard()
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="翻訳するテキストがありません。")
    try:
        return {"translated": ai_util.translate(text, payload.target_lang.strip() or "日本語")}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/openchats/{openchat_id}/chat")
def chat_openchat(openchat_id: int, payload: ChatPayload, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_oc_member(session, openchat_id, user.id)
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    transcript = _oc_transcript(session, openchat_id)
    try:
        reply = ai_util.chat_with_context(config.AI_ASSISTANT_NAME, transcript, message)
        return {"reply": reply, "assistant_name": config.AI_ASSISTANT_NAME}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/dm/{conv_id}/chat")
def chat_dm(conv_id: int, payload: ChatPayload, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _guard()
    _require_dm(session, conv_id, user.id)
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="メッセージが空です。")
    transcript = _dm_transcript(session, conv_id)
    try:
        reply = ai_util.chat_with_context(config.AI_ASSISTANT_NAME, transcript, message)
        return {"reply": reply, "assistant_name": config.AI_ASSISTANT_NAME}
    except ai_util.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
