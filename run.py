"""Discloud起動用エントリーポイント。

Discloudの discloud.config は Python ファイルを直接実行する形式のため、
uvicornをこのファイルから起動する。ポートは Discloud が割り当てる
環境変数 PORT を優先する。

Dev yuzuki_akrdev.ofc
"""

import os
import uvicorn

if __name__ == "__main__":
    port = None
    try:
        from app import config as _config
        cfg_port = _config.setting("APP_PORT")
        if cfg_port and str(cfg_port).isdigit():
            port = int(cfg_port)
    except Exception:
        port = None
    if port is None:
        port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
