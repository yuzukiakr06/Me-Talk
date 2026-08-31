"""MIRAI Chat の Cloudflare Tunnel 起動。

MIRAI ID(mirai-casino アプリ)と同じ方式。Discloud上で起動時に cloudflared バイナリを
取得し、cloudflared_token.txt または環境変数のトークンでトンネルを張る。
トークン未設定なら何もしない(ローカル動作確認時はトンネル無しで問題ない)。

Dev yuzuki_akrdev.ofc
"""

import platform
import stat
import subprocess
import urllib.request
from pathlib import Path

from app import config

BASE_DIR = Path(__file__).resolve().parent.parent
_process: subprocess.Popen | None = None


def _cloudflared_url() -> str:
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    return "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"


def _ensure_binary() -> Path:
    binary = Path("/tmp/cloudflared")
    if binary.exists():
        return binary
    with urllib.request.urlopen(_cloudflared_url(), timeout=60) as response:
        binary.write_bytes(response.read())
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return binary


def start() -> None:
    global _process
    if _process:
        return
    token_file = BASE_DIR / "cloudflared_token.txt"
    token = config.setting("CLOUDFLARED_TOKEN")
    if not token and token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        print("[cloudflared] トークン未設定のためトンネルは起動しません")
        return
    try:
        binary = _ensure_binary()
        _process = subprocess.Popen(
            [str(binary), "tunnel", "--no-autoupdate", "run", "--token", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        print("[cloudflared] トンネルを起動しました")
    except Exception as exc:
        print(f"[cloudflared] 起動失敗: {exc}")
