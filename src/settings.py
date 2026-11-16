"""設定の値を読む。

本番（Cloudflare Workers）では、設定と秘密情報は request.scope["env"] に入っている。
手元（Uvicorn）にはそれがないので、環境変数から読む。
"""

import os

from fastapi import Request


def setting(request: Request, name: str) -> str | None:
    env = request.scope.get("env")
    if env is not None:
        value = getattr(env, name, None)
        if value:
            return str(value)
    return os.environ.get(name) or None
