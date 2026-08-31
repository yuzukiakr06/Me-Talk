"""Me Talk グループ通話の中継(Cloudflare Realtime SFU)。

App Secret はブラウザに渡せないため、Me Talk のサーバーが仲介する。
端末は Me Talk にだけ話しかけ、Me Talk が Cloudflare の API を代理で叩く。

やり取りの流れ:
  1. 端末が接続の申し出(offer)を作る
  2. /sessions/new に offer を渡してセッションを作り、応答(answer)を受け取る
     ここで offer は必須。空で送ると decoding_error になる
  3. 端末が answer を適用したあと、あらためて offer を作り直す
     応答を受けてコーデックが確定するため、最新の状態を伝え直す必要がある
  4. /tracks/new にその offer とトラック名を渡して送出を確定させる
  5. 他の人が入ってきたら、その人のトラックを /tracks/new で引き込む
     このとき Cloudflare 側から offer が来るので、端末の answer を /renegotiate へ返す
  6. 抜けるときは /tracks/close

Dev yuzuki_akrdev.ofc
"""

import requests

from app import config

BASE = "https://rtc.live.cloudflare.com/v1/apps"
TIMEOUT = 12


class SfuError(Exception):
    """Cloudflare 側から想定外の応答が返ってきたときに投げる。"""


def enabled() -> bool:
    return bool(config.SFU_APP_ID and config.SFU_APP_SECRET)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.SFU_APP_SECRET}",
        "Content-Type": "application/json",
    }


def _url(path: str) -> str:
    return f"{BASE}/{config.SFU_APP_ID}{path}"


def _call(method: str, path: str, body: dict | None = None) -> dict:
    if not enabled():
        raise SfuError("グループ通話の中継が設定されていません。")
    try:
        res = requests.request(method, _url(path), headers=_headers(), json=body or {}, timeout=TIMEOUT)
    except Exception as exc:
        raise SfuError(f"中継サーバーに接続できませんでした: {exc}")
    if res.status_code not in (200, 201):
        raise SfuError(f"中継サーバーがエラーを返しました ({res.status_code}): {res.text[:200]}")
    try:
        data = res.json()
    except Exception:
        raise SfuError("中継サーバーの応答を解釈できませんでした。")
    error = data.get("errorDescription") or data.get("errorCode")
    if error:
        raise SfuError(f"中継サーバーがエラーを返しました: {error}")
    return data


def new_session(offer_sdp: str) -> dict:
    """セッションを作る。申し出(offer)は必須。"""
    if not offer_sdp:
        raise SfuError("接続の申し出がありません。")
    return _call("POST", "/sessions/new", {
        "sessionDescription": {"type": "offer", "sdp": offer_sdp},
    })


def verify_credentials() -> tuple:
    """App ID と App Secret が有効か確かめる。

    セッションの作成には実際の申し出が要るため、
    存在しないセッションを参照して認証だけを試す。
    認証に失敗すれば 401 か 403 が返り、それ以外なら鍵は有効。
    """
    if not enabled():
        return False, "未設定"
    try:
        res = requests.get(
            _url("/sessions/00000000000000000000000000000000"),
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except Exception as exc:
        return False, f"接続できませんでした: {exc}"
    if res.status_code in (401, 403):
        return False, "App ID または App Secret が正しくありません"
    return True, ""


def publish_tracks(session_id: str, offer_sdp: str, tracks: list) -> dict:
    """自分の音声や映像を送り出す。

    tracks は [{"location": "local", "mid": "0", "trackName": "..."}] の形。
    """
    return _call("POST", f"/sessions/{session_id}/tracks/new", {
        "sessionDescription": {"type": "offer", "sdp": offer_sdp},
        "tracks": tracks,
    })


def subscribe_tracks(session_id: str, tracks: list) -> dict:
    """他の人のトラックを引き込む。

    tracks は [{"location": "remote", "sessionId": "...", "trackName": "..."}] の形。
    Cloudflare 側から offer が返るので、呼び出し元は answer を renegotiate へ返す。
    """
    return _call("POST", f"/sessions/{session_id}/tracks/new", {"tracks": tracks})


def renegotiate(session_id: str, answer_sdp: str) -> dict:
    """Cloudflare からの申し出に対する返事を渡す。"""
    return _call("PUT", f"/sessions/{session_id}/renegotiate", {
        "sessionDescription": {"type": "answer", "sdp": answer_sdp},
    })


def close_tracks(session_id: str, track_names: list, force: bool = False) -> dict:
    """トラックを閉じる。"""
    return _call("PUT", f"/sessions/{session_id}/tracks/close", {
        "tracks": [{"mid": name} if name.startswith("#") else {"trackName": name} for name in track_names],
        "force": force,
    })


def session_info(session_id: str) -> dict:
    return _call("GET", f"/sessions/{session_id}")
