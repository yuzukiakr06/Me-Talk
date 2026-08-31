"""Me Talk AIアシスト用のAnthropic APIクライアント。

Me TalkサーバーからClaude APIを呼び、要約・翻訳・返信案・予定抽出を行う。
APIキーは config.ANTHROPIC_API_KEY(環境変数 ANTHROPIC_API_KEY)から読む。
未設定時は AIUnavailable を送出し、上位で「AI機能は未設定」と案内する。

Dev yuzuki_akrdev.ofc
"""

import json

import requests

from app import config

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
TIMEOUT = 40


class AIUnavailable(Exception):
    pass


class AIError(Exception):
    pass


def is_configured() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def call_claude(system: str, user_content: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    if not config.ANTHROPIC_API_KEY:
        raise AIUnavailable("AI機能が設定されていません。")
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, data=json.dumps(body), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AIError(f"AIサーバーに接続できませんでした: {exc}")
    if resp.status_code != 200:
        raise AIError(f"AI応答エラー(status {resp.status_code})")
    data = resp.json()
    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "\n".join(parts).strip()
    if not text:
        raise AIError("AIから有効な応答が得られませんでした。")
    return text


def summarize(transcript: str) -> str:
    system = (
        "あなたはチャットグループの会話を要約するアシスタントです。"
        "以下の会話ログを、重要な論点・決定事項・未解決の点がわかるように日本語で簡潔にまとめてください。"
        "箇条書きを使い、全体で5行以内にしてください。"
    )
    return call_claude(system, transcript, max_tokens=700)


def translate(text: str, target_lang: str) -> str:
    system = (
        f"あなたは翻訳者です。与えられたテキストを{target_lang}に自然に翻訳し、"
        "翻訳結果のみを出力してください。前置きや説明は不要です。"
    )
    return call_claude(system, text, max_tokens=700)


def reply_suggestions(transcript: str) -> list[str]:
    system = (
        "あなたはチャットの返信を提案するアシスタントです。"
        "以下の会話の流れに対して、自然な返信案を3つ提案してください。"
        "各案は1〜2文で、日本語で、JSON配列(文字列のみ)として出力してください。"
        "余計な説明やコードフェンスは付けず、JSON配列だけを返してください。"
    )
    raw = call_claude(system, transcript, max_tokens=500)
    return _parse_str_array(raw)


def topic_suggestions(transcript: str) -> list[str]:
    system = (
        "あなたはチャットの話題を提案するアシスタントです。"
        "以下の会話の流れをふまえ、次に話すと盛り上がりそうな話題や質問を3つ提案してください。"
        "各案は短い一文で、日本語で、JSON配列(文字列のみ)として出力してください。"
        "会話が止まっている場合でも自然に続けられる話題を出してください。JSON配列だけを返してください。"
    )
    raw = call_claude(system, transcript, max_tokens=500)
    return _parse_str_array(raw)


def analyze_mood(transcript: str) -> str:
    system = (
        "あなたはチャットの雰囲気を分析するアシスタントです。"
        "以下の会話全体のムード(盛り上がり・感情のトーン・話題の傾向)を、"
        "日本語で3〜4行で簡潔にまとめてください。決めつけず、やわらかい表現で。箇条書きでも構いません。"
    )
    return call_claude(system, transcript, max_tokens=500)


def _parse_str_array(raw: str) -> list[str]:
    cleaned = raw.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return [str(x) for x in arr][:5]
    except (json.JSONDecodeError, TypeError):
        pass
    lines = [ln.strip("-・ 　") for ln in raw.splitlines() if ln.strip()]
    return lines[:5]


def extract_events(transcript: str) -> list[dict]:
    system = (
        "あなたは会話から予定を抽出するアシスタントです。"
        "以下の会話から、日時が特定できる予定を抽出してください。"
        "各予定を {\"title\": ..., \"datetime\": \"YYYY-MM-DD HH:MM\", \"note\": ...} の形式で、"
        "JSON配列として出力してください。日時が曖昧なものは含めないでください。"
        "予定がなければ空配列 [] を返してください。JSON以外は出力しないでください。"
    )
    raw = call_claude(system, transcript, max_tokens=700)
    cleaned = raw.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            result = []
            for item in arr:
                if isinstance(item, dict) and item.get("title") and item.get("datetime"):
                    result.append({
                        "title": str(item["title"])[:80],
                        "datetime": str(item["datetime"])[:20],
                        "note": str(item.get("note", ""))[:200],
                    })
            return result[:10]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _mirei_system(assistant_name: str) -> str:
    return (
        f"あなたは「{assistant_name}」という名前の、チャットアプリ「Me Talk」に組み込まれたAIアシスタントです。"
        "親しみやすく丁寧な日本語で、簡潔に答えます。"
        "あなたの役割は、このチャットでの会話の補助です。具体的には次のような依頼に答えます: "
        "会話の要約、翻訳、返信文の作成や下書き、会話に出た予定やタスクの整理、"
        "チャットの内容についての質問、言い回しや文章の相談、Me Talkの使い方の案内。"
        "会話の文脈(直前のトーク履歴)を踏まえて答えてください。"
        "ただし、チャットの補助と関係のない話題(一般的な雑学、時事、プログラミング相談、"
        "宿題の代行、個人的な悩み相談、あなた自身やAIについての一般論など)を求められた場合は、"
        "答えずに『そのご質問にはお答えできません。私はこのトークの会話をお手伝いするアシスタントです。』"
        "と丁寧に断ってください。危険・違法・有害な依頼にも応じないでください。"
        "回答は基本的に200文字以内で簡潔にまとめてください。"
    )


def chat_with_context(assistant_name: str, transcript: str, user_message: str) -> str:
    system = _mirei_system(assistant_name)
    content = (
        "これまでのトーク履歴:\n"
        f"{transcript}\n\n"
        "----\n"
        f"ユーザーからあなた({assistant_name})への依頼・質問:\n{user_message}\n\n"
        "上の依頼に、Me Talkのアシスタントとして答えてください。"
        "チャット補助と無関係なら、指示どおり丁寧にお断りしてください。"
    )
    return call_claude(system, content, max_tokens=600)
