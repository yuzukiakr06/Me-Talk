"""MIRAI Chat アプリケーション エントリーポイント。

フェーズ1: 認証 + ユーザー基盤。

Dev yuzuki_akrdev.ofc
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import config
from app.db import init_db
from app import cloudflared
from app.routes_auth import router as auth_router
from app.routes_openchat import router as openchat_router
from app.routes_ws import router as ws_router
from app.routes_upload import router as upload_router, UPLOAD_DIR
from app.routes_dm import router as dm_router
from app.routes_profile import router as profile_router
from app.routes_friends import router as friends_router
from app.routes_ai import router as ai_router
from app.routes_notifications import router as notifications_router
from app.routes_reactions import router as reactions_router
from app.routes_admin import router as admin_router
from app.routes_support import router as support_router
from app.routes_2fa import router as twofactor_router
from app.routes_passkey import router as passkey_router
from app.routes_schedule import router as schedule_router
from app.routes_posts import router as posts_router, follow_router
from app.routes_blocks import router as blocks_router
from app.routes_calls import router as calls_router

app = FastAPI(title=config.SITE_NAME)

app.include_router(auth_router)
app.include_router(openchat_router)
app.include_router(ws_router)
app.include_router(upload_router)
app.include_router(dm_router)
app.include_router(profile_router)
app.include_router(friends_router)
app.include_router(ai_router)
app.include_router(notifications_router)
app.include_router(reactions_router)
app.include_router(admin_router)
app.include_router(support_router)
app.include_router(twofactor_router)
app.include_router(passkey_router)
app.include_router(schedule_router)
app.include_router(posts_router)
app.include_router(follow_router)
app.include_router(blocks_router)
app.include_router(calls_router)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.on_event("startup")
def on_startup() -> None:
    _check_encryption()
    init_db()
    _verify_encryption()
    _encrypt_existing()
    _prepare_users()
    _sync_badges()
    _start_schedule_worker()
    _check_turn()
    _check_sfu()
    cloudflared.start()


def _check_turn() -> None:
    """通話の中継サーバーが使える状態か起動時に確かめてログに出す。

    ここで一度取得しておくと、最初の通話の待ち時間も短くなる。
    """
    from app import config
    if not config.TURN_KEY_ID or not config.TURN_KEY_API_TOKEN:
        print("[call] TURN未設定: 回線によっては通話がつながりません")
        return
    try:
        from app.routes_calls import _cloudflare_turn
        servers = _cloudflare_turn()
        if servers:
            urls = []
            for entry in servers:
                raw = entry.get("urls")
                urls.extend(raw if isinstance(raw, list) else [raw])
            relays = [u for u in urls if u and (u.startswith("turn:") or u.startswith("turns:"))]
            print(f"[call] TURN有効: 中継 {len(relays)}件 を取得しました")
        else:
            print("[call] TURN取得失敗: 鍵を確認してください")
    except Exception as exc:
        print(f"[call] TURNの確認に失敗しました: {exc}")


def _check_sfu() -> None:
    """グループ通話の中継が使えるか起動時に確かめる。

    試しにセッションを1つ作ってすぐ捨てる。
    鍵の誤りはここで分かるので、通話中に初めて気づく事態を避けられる。
    """
    from app import config, sfu
    if not sfu.enabled():
        print(f"[call] グループ通話は直接方式 (最大{config.CALL_MAX_PARTICIPANTS}人): SFU未設定")
        return
    try:
        ok, reason = sfu.verify_credentials()
        if ok:
            print(f"[call] SFU有効: グループ通話は中継方式 (最大{config.SFU_MAX_PARTICIPANTS}人)")
        else:
            print(f"[call] SFU利用不可: {reason}")
    except Exception as exc:
        print(f"[call] SFU接続確認に失敗しました: {exc}")


def _start_schedule_worker() -> None:
    """予約送信とリマインドのワーカーを動かす。"""
    import asyncio
    from app.routes_schedule import worker
    try:
        asyncio.get_running_loop().create_task(worker())
    except RuntimeError:
        print("[schedule] イベントループが無いためワーカーは起動しません")


def _check_encryption() -> None:
    """暗号化の設定を確かめる。鍵はあるのに使えない状態なら起動を止める。"""
    from app import crypto
    reason = crypto.problem()
    if reason:
        raise RuntimeError(
            "保存時暗号化を有効にできません: " + reason
            + " / config.json の ENCRYPTION_KEY を確認するか、"
            "暗号化を使わない場合は ENCRYPTION_KEY を空にしてください。"
        )
    if crypto.enabled():
        print("[crypto] 保存時暗号化: 有効")
    else:
        print("[crypto] 保存時暗号化: 無効 (ENCRYPTION_KEY 未設定)")


def _verify_encryption() -> None:
    """既存の暗号文を実際に復号できるか確かめる。"""
    from app import migrations
    from app.db import engine
    migrations.verify_encryption_key(engine)


def _encrypt_existing() -> None:
    """既存の平文データを暗号化する。"""
    from app import migrations
    from app.db import engine
    try:
        migrations.encrypt_existing(engine)
    except Exception as exc:
        print(f"[crypto] 既存データの暗号化に失敗しました: {exc}")


def _prepare_users() -> None:
    """ユーザー名の付け替えと、ユーザーIDの遡り採番を行う。"""
    from app import migrations
    from app.db import engine
    from sqlmodel import Session
    try:
        with Session(engine) as session:
            renamed = migrations.apply_username_renames(session)
            filled = migrations.backfill_user_ids(session)
        if renamed:
            print(f"[users] ユーザー名を変更しました: {', '.join(renamed)}")
        if filled:
            print(f"[users] ユーザーIDを新たに付与しました: {filled}件")
    except Exception as exc:
        print(f"[users] 初期処理に失敗しました: {exc}")


def _sync_badges() -> None:
    from app.badges import sync_developer_badges
    from app.db import engine
    from sqlmodel import Session
    try:
        with Session(engine) as session:
            result = sync_developer_badges(session)
        if result["granted"]:
            print(f"[badges] 開発者バッジを付与: {', '.join(result['granted'])}")
        if result["revoked"]:
            print(f"[badges] 開発者バッジを剥奪: {', '.join(result['revoked'])}")
    except Exception as exc:
        print(f"[badges] 同期に失敗しました: {exc}")


@app.get("/")
def index():
    html = (STATIC_DIR / "landing.html").read_text(encoding="utf-8")
    html = html.replace("__BASE_URL__", config.BASE_URL.rstrip("/"))
    return HTMLResponse(content=html)


@app.get("/app")
def chat_app():
    return FileResponse(
        STATIC_DIR / "app.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC_DIR / "pwa/metalk-96.png", media_type="image/png")


@app.get("/legal/terms")
def legal_terms():
    return FileResponse(STATIC_DIR / "legal/terms.html")


@app.get("/legal/privacy")
def legal_privacy():
    return FileResponse(STATIC_DIR / "legal/privacy.html")


@app.get("/health")
def health():
    return {"ok": True, "service": config.SITE_NAME, "version": config.APP_VERSION}


@app.get("/api/app-info")
def app_info():
    return {"site_name": config.SITE_NAME, "version": config.APP_VERSION}
