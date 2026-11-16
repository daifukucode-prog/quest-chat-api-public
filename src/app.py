"""FastAPI 本体。手元（Uvicorn）と本番（Cloudflare Workers）で同じものを使う。

このファイルには Workers だけの部品を書かない。
Workers につなぐ部分は worker.py に分けてある。
"""

import json
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

import chat as chat_flow
import guard
import llm
import store
from content import ContentError
from content import store as content_store
from guard import ApiError
from schemas import ChatRequest, FeedbackRequest
from settings import setting

app = FastAPI(title="quest-chat-api")

# 手元で使う SQLite のファイル（本番は D1 を使うので、ここは通らない）
DEFAULT_SQLITE_PATH = "build/dev.sqlite3"


# ---- 共通の部品 ----


def open_db(request: Request) -> store.Database:
    """本番は D1、手元は SQLite を返す。使い終わったら close_db で閉じる。"""
    env = request.scope.get("env")
    d1 = getattr(env, "DB", None) if env is not None else None
    if d1 is not None:
        return store.D1Database(d1)
    return store.SqliteDatabase(setting(request, "SQLITE_PATH") or DEFAULT_SQLITE_PATH)


def close_db(db: store.Database) -> None:
    """手元の SQLite を閉じる（D1 は閉じる必要がない）。"""
    if isinstance(db, store.SqliteDatabase):
        db.close()


def make_llm(api_key: str):
    """Claude を呼ぶ入れ物を作る。テストでは、ここを偽物に差し替える。"""
    return llm.Llm(api_key)


def sse(event: str, data: dict) -> str:
    """知らせ1つを、Server-Sent Events の形の文字にする。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def cors_headers(request: Request) -> dict[str, str]:
    """許可したサイトのブラウザが、返事を読めるようにするための見出し。"""
    origin = guard.allowed_origin(request.headers.get("origin"), setting(request, "ALLOWED_ORIGINS"))
    if origin is None:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }


def require_allowed_origin(request: Request) -> None:
    """許可していないサイトからの呼び出しを断る（要件 F-42）。"""
    if not cors_headers(request):
        raise ApiError("forbidden_origin")


@app.exception_handler(ApiError)
async def on_api_error(request: Request, error: ApiError):
    return JSONResponse(
        {"error": error.kind, "message": error.message},
        status_code=error.status,
        headers=cors_headers(request),
    )


@app.exception_handler(RequestValidationError)
async def on_invalid_input(request: Request, error: RequestValidationError):
    # 形が違う・長すぎるときは 400 で返す（設計書 5.4）。送られた中身は返事に含めない
    return await on_api_error(request, ApiError("invalid_input"))


# ---- 動いているか確かめる ----


@app.get("/api/health")
async def health(request: Request):
    """動いているかを確かめるためのURL（設計書 5.3）。

    資料を読み込めているかと、その版を返す。
    資料を読み込めなくても、API 自体が動いていれば 200 を返す。
    """
    try:
        content = await content_store.get(setting(request, "CONTENT_URL"), time.time())
    except ContentError as e:
        print(f"health: {e}")
        return {"ok": True, "content_loaded": False, "content_version": None}
    return {
        "ok": True,
        "content_loaded": True,
        "content_version": content.version,
        "chunks": len(content.chunks),
        "glossary": len(content.glossary),
    }


# ---- 質問する ----


@app.options("/api/chat", dependencies=[Depends(require_allowed_origin)])
@app.options("/api/chat/feedback", dependencies=[Depends(require_allowed_origin)])
async def preflight(request: Request):
    """ブラウザが本番の送信の前に送ってくる確認に答える。"""
    return Response(status_code=204, headers=cors_headers(request))


@app.post("/api/chat", dependencies=[Depends(require_allowed_origin)])
async def chat(request: Request, body: ChatRequest):
    """質問を受けて、答えを順に届ける（設計書 3章・5.1）。

    ① 入力の確認・緊急停止・伏せ字・回数（ここまでは、断るときに状態コードで返す）
    ②〜⑤ 資料の読み込み・資料選び・回答（ここからは、知らせを順に届ける）
    ⑥ 記録
    """
    if (setting(request, "CHAT_ENABLED") or "true").lower() == "false":
        raise ApiError("maintenance")  # 緊急停止（要件 F-44）

    # 時刻と乱数は、質問を受けてから作る（Workers では関数の外で作ると 0 になる）
    now = time.time()
    log_id = str(uuid.uuid4())
    # 伏せ字は、AIに送る前と記録する前の両方に効くよう、ここで1回かける
    question = guard.redact(body.question)
    history = [item.model_copy(update={"text": guard.redact(item.text)}) for item in body.history]

    # ② 資料とキーがそろっているかを、回数を数える前に確かめる（そろっていないのに1回ぶん減らさないため）
    api_key = setting(request, "ANTHROPIC_API_KEY")
    try:
        content = await content_store.get(setting(request, "CONTENT_URL"), now)
    except ContentError as e:
        print(f"chat: {e}")
        raise ApiError("unavailable") from e
    if not api_key:
        print("chat: ANTHROPIC_API_KEY が設定されていません")
        raise ApiError("unavailable")

    db = open_db(request)
    try:
        await guard.count_question(db, body.browser_id, now)
    except BaseException:
        close_db(db)
        raise

    outcome = chat_flow.Outcome()

    async def events():
        try:
            async for event, data in chat_flow.respond(
                make_llm(api_key), content, question, body.context, history, log_id, outcome, time.time
            ):
                yield sse(event, data)
        finally:
            # ⑥ 記録。答えの本文・直前のやりとり・ブラウザのIDは保存しない
            if outcome.error_reason:
                print(f"chat: {outcome.error_reason}")
            try:
                await store.save_log(
                    db,
                    id=log_id,
                    created_at=guard.utc_iso(now),
                    question=question,
                    course=body.context.course,
                    set_id=body.context.set_id,
                    screen=body.context.screen,
                    chunk_ids=outcome.chunk_ids,
                    result=outcome.result,
                    latency_first_ms=outcome.first_ms,
                    latency_total_ms=outcome.total_ms,
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                    content_version=content.version,
                )
            finally:
                close_db(db)

    headers = {"Cache-Control": "no-cache", **cors_headers(request)}
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


# ---- 評価を送る ----


@app.post("/api/chat/feedback", dependencies=[Depends(require_allowed_origin)])
async def feedback(request: Request, body: FeedbackRequest):
    """「役に立った／立たなかった」を記録する（設計書 5.2）。"""
    db = open_db(request)
    try:
        found = await store.set_helpful(db, body.log_id, body.helpful)
    finally:
        close_db(db)
    if not found:
        raise ApiError("not_found")
    return JSONResponse({"ok": True}, headers=cors_headers(request))
