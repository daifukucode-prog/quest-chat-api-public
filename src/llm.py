"""Claude API の呼び出し。設計書 3章の④⑤で使う。

モデルの名前と設定は、このファイルにだけ書く。
ほかのモデルと比べたくなったら、ここを差し替える（判断の記録 D-12）。

Claude API の公式の部品（anthropic）は使わず、HTTP で直接呼ぶ（設計書 2章 V-2 の代わりの案）。
公式の部品は大きく、Cloudflare Workers の上限に収まらなかったため（判断の記録 D-25 の追記）：
  - 起動のときに読み込むと、メモリの上限を超えて公開できない
  - 質問を受けてから読み込むと、読み込むだけで計算の時間を約2秒使い、上限で止められる
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2

# 資料選びも回答も Claude Haiku 5.5（判断の記録 D-12）
MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# 1回の呼び出しで待つ長さの上限（秒）
TIMEOUT_SEC = 30
# 混み合っているときなどに、やり直す回数と、やり直す前に待つ秒数
MAX_RETRIES = 2
RETRY_WAIT_SEC = (0.5, 1.5)
# やり直してよい状態コード（回数上限、混雑、サーバー側の失敗）
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LlmError(Exception):
    """AIから答えをもらえなかった。

    kind は断り方の種類（guard.ERRORS の名前）。
      busy        … Claude API が混み合っている（やり直してもだめだった）
      unavailable … それ以外（通信の失敗、キーの間違い、AIが答えを断った、など）
    reason は記録やログ用の短い理由。利用者には見せない。
    """

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass
class Usage:
    """AIに渡した量と、AIが書いた量（トークン）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # input_tokens のうち、覚えさせてあった分（料金が約10分の1）

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens


def usage_from(data: dict) -> Usage:
    """Claude API が返す usage を、このAPIの数え方に直す。入力は、覚えていた分・覚えさせた分も足す。"""
    read = data.get("cache_read_input_tokens") or 0
    written = data.get("cache_creation_input_tokens") or 0
    return Usage((data.get("input_tokens") or 0) + read + written, data.get("output_tokens") or 0, read)


def error_for_status(status: int) -> LlmError:
    """Claude API の状態コードを、このAPIの断り方に直す。"""
    if status in (429, 529):
        return LlmError("busy", f"api_status_{status}")
    return LlmError("unavailable", f"api_status_{status}")


def cached(system: str) -> list[dict]:
    """指示文を、Claude 側に一時的に覚えさせる形にする（プロンプトキャッシュ）。

    指示文と目次は毎回同じなので、続けて質問が来たときは、同じ部分の料金と時間が減る。
    """
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class SseReader:
    """少しずつ届く通信のかたまりから、知らせ（data: の行の JSON）を取り出す。

    かたまりは、行や文字の途中で切れて届くことがある。行がそろうまでためておく。
    Cloudflare Workers では bytes ではなく memoryview で届くので、最初に bytes に直す。
    """

    def __init__(self) -> None:
        self._held = b""

    def feed(self, chunk) -> list[dict]:
        self._held += bytes(chunk)
        *lines, self._held = self._held.split(b"\n")
        events = []
        for line in lines:
            text = line.decode("utf-8").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:]))
        return events


class Llm:
    """Claude を呼ぶ入れ物。テストでは、同じ形の偽物に差し替える。"""

    def __init__(self, api_key: str, transport=None) -> None:
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        self._transport = transport  # テストで、通信を偽物に差し替えるためのもの

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=TIMEOUT_SEC, transport=self._transport)

    def _body(self, system: str, user: str, max_tokens: int, output_config: dict) -> dict:
        return {
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": cached(system),
            "messages": [{"role": "user", "content": user}],
            "output_config": output_config,
        }

    async def ask_json(self, system: str, user: str, schema: dict, max_tokens: int, effort: str) -> tuple[dict, Usage]:
        """決まった形（JSON）で答えさせる。資料選びで使う。

        effort は考える量（low / medium / high）。少ないほど速い。
        """
        body = self._body(
            system,
            user,
            max_tokens,
            {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        async with self._client() as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.post(API_URL, headers=self._headers, json=body)
                except httpx2.HTTPError as e:
                    error = LlmError("unavailable", f"connection_{type(e).__name__}")
                else:
                    if response.status_code == 200:
                        break
                    error = error_for_status(response.status_code)
                    if response.status_code not in RETRY_STATUS:
                        raise error
                if attempt == MAX_RETRIES:
                    raise error
                await asyncio.sleep(RETRY_WAIT_SEC[attempt])
            message = response.json()
        if message.get("stop_reason") != "end_turn":
            # refusal（AIが断った）や max_tokens（途中で切れた）のときは、JSON として使えない
            raise LlmError("unavailable", f"stop_{message.get('stop_reason')}")
        text = next((b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"), "")
        try:
            return json.loads(text), usage_from(message.get("usage", {}))
        except ValueError as e:
            raise LlmError("unavailable", "bad_json") from e

    async def stream_text(
        self, system: str, user: str, max_tokens: int, effort: str, usage: Usage
    ) -> AsyncIterator[str]:
        """答えの文字を、できた順に少しずつ返す。回答で使う。

        使った量は、終わったときに usage に足す。
        やり直すのは、文字を1つも返していないうちだけ（途中からやり直すと、答えが二重になるため）。
        """
        body = self._body(system, user, max_tokens, {"effort": effort}) | {"stream": True}
        spent: dict = {}
        stop_reason = None
        async with self._client() as client:
            for attempt in range(MAX_RETRIES + 1):
                started = False
                try:
                    async with client.stream("POST", API_URL, headers=self._headers, json=body) as response:
                        if response.status_code != 200:
                            await response.aread()
                            error = error_for_status(response.status_code)
                            if response.status_code not in RETRY_STATUS:
                                raise error
                        else:
                            started = True
                            reader = SseReader()
                            async for chunk in response.aiter_bytes():
                                for event in reader.feed(chunk):
                                    kind = event.get("type")
                                    if kind == "message_start":
                                        spent.update(event.get("message", {}).get("usage", {}))
                                    elif kind == "content_block_delta":
                                        delta = event.get("delta", {})
                                        if delta.get("type") == "text_delta":
                                            yield delta.get("text", "")
                                    elif kind == "message_delta":
                                        spent.update(event.get("usage", {}))
                                        stop_reason = event.get("delta", {}).get("stop_reason")
                                    elif kind == "error":
                                        # 流れの途中で Claude API が失敗を知らせてきた
                                        name = event.get("error", {}).get("type", "error")
                                        busy = name in ("overloaded_error", "rate_limit_error")
                                        raise LlmError("busy" if busy else "unavailable", f"stream_{name}")
                            break
                except httpx2.HTTPError as e:
                    error = LlmError("unavailable", f"connection_{type(e).__name__}")
                    if started:
                        raise error from e
                if attempt == MAX_RETRIES:
                    raise error
                await asyncio.sleep(RETRY_WAIT_SEC[attempt])
        usage.add(usage_from(spent))
        if stop_reason == "refusal":
            raise LlmError("unavailable", "stop_refusal")
