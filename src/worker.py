"""本番の入口。FastAPI 本体（app.py）を Cloudflare Workers につなぐだけ。

手元で Uvicorn を使うときは、このファイルを通らない。
"""

from workers import WorkerEntrypoint

from app import app


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # Workers に入っている ASGI サーバー。本番で Uvicorn の代わりをする
        import asgi

        # self.env（設定と秘密情報）は、FastAPI の中で request.scope["env"] として読める
        return await asgi.fetch(app, request.js_object, self.env)
