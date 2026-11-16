"""Claude API の呼び出し（llm.py）のテスト。通信は偽物に差し替える（本物の API は呼ばない）。"""

import asyncio
import json

import httpx2
import pytest

import llm
from llm import Llm, LlmError, SseReader, Usage


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_WAIT_SEC", (0, 0))


def sse(*events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events).encode()


STREAM = sse(
    {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            }
        },
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "考え中"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "ターミナルは、"}},
    {"type": "ping"},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "黒い画面です。"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
)


def fake_api(*replies):
    """決めておいた返事を順に返す偽物の通信。送られてきたものを覚えておく。"""
    queue = list(replies)
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx2.MockTransport(handler), seen


def json_reply(payload: dict, stop_reason="end_turn", status=200) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(payload)}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 300, "cache_read_input_tokens": 5000, "output_tokens": 20},
        },
    )


def ask(client: Llm):
    return asyncio.run(client.ask_json("指示文", "質問", {"type": "object"}, max_tokens=100, effort="low"))


def stream(client: Llm):
    usage = Usage()

    async def collect():
        return [piece async for piece in client.stream_text("指示文", "質問", 100, "low", usage)]

    return asyncio.run(collect()), usage


# ---- 少しずつ届く知らせを読む ----


def test_reader_handles_chunks_cut_anywhere():
    # 行の途中、文字（日本語は3バイト）の途中で切れても、同じ結果になる
    for size in (1, 2, 3, 7, 50, len(STREAM)):
        reader = SseReader()
        events = []
        for i in range(0, len(STREAM), size):
            events += reader.feed(STREAM[i : i + size])
        texts = [e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta"]
        assert texts == ["ターミナルは、", "黒い画面です。"], size


def test_reader_accepts_memoryview_as_workers_gives():
    assert SseReader().feed(memoryview(sse({"type": "ping"}))) == [{"type": "ping"}]


# ---- 答えを順に受け取る ----


def test_stream_yields_only_answer_text_and_counts_usage():
    transport, seen = fake_api(httpx2.Response(200, content=STREAM))
    pieces, usage = stream(Llm("test-key", transport))
    assert pieces == ["ターミナルは、", "黒い画面です。"]  # 「考え中」は答えに混ぜない
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (5100, 42, 5000)


def test_request_has_key_model_cached_system_and_no_sampling_params():
    transport, seen = fake_api(httpx2.Response(200, content=STREAM))
    stream(Llm("test-key", transport))
    request = seen[0]
    body = json.loads(request.content)
    assert request.headers["x-api-key"] == "test-key" and request.headers["anthropic-version"] == "2023-06-01"
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert body["model"] == "claude-haiku-5-5" and body["stream"] is True
    assert body["system"] == [{"type": "text", "text": "指示文", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "質問"}]
    assert body["output_config"] == {"effort": "low"}
    assert not {"temperature", "top_p", "top_k"} & set(body)  # このモデルでは使えない


def test_stream_retries_before_any_text_then_succeeds():
    transport, seen = fake_api(httpx2.Response(529), httpx2.ConnectError("x"), httpx2.Response(200, content=STREAM))
    pieces, _ = stream(Llm("k", transport))
    assert pieces == ["ターミナルは、", "黒い画面です。"] and len(seen) == 3


def test_stream_gives_up_as_busy_after_retries():
    transport, seen = fake_api(*[httpx2.Response(429)] * 3)
    with pytest.raises(LlmError) as e:
        stream(Llm("k", transport))
    assert e.value.kind == "busy" and len(seen) == 3


def test_wrong_key_is_not_retried():
    transport, seen = fake_api(httpx2.Response(401))
    with pytest.raises(LlmError) as e:
        stream(Llm("k", transport))
    assert e.value.kind == "unavailable" and e.value.reason == "api_status_401" and len(seen) == 1


def test_error_event_in_the_middle_of_the_stream():
    body = sse(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "途中まで"}},
        {"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
    )
    transport, _ = fake_api(httpx2.Response(200, content=body))
    with pytest.raises(LlmError) as e:
        stream(Llm("k", transport))
    assert e.value.kind == "busy"


def test_refusal_is_an_error_after_the_text():
    body = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {"output_tokens": 0}},
    )
    transport, _ = fake_api(httpx2.Response(200, content=body))
    with pytest.raises(LlmError) as e:
        stream(Llm("k", transport))
    assert e.value.reason == "stop_refusal"


# ---- 決まった形で答えさせる ----


def test_ask_json_returns_parsed_json_and_usage():
    transport, seen = fake_api(json_reply({"related": True, "numbers": [3, 1]}))
    data, usage = ask(Llm("k", transport))
    assert data == {"related": True, "numbers": [3, 1]}
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (5300, 20, 5000)
    body = json.loads(seen[0].content)
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "stream" not in body


def test_ask_json_retries_then_succeeds():
    transport, seen = fake_api(httpx2.Response(503), json_reply({"related": False, "numbers": []}))
    data, _ = ask(Llm("k", transport))
    assert data["related"] is False and len(seen) == 2


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_ask_json_refuses_incomplete_answers(stop):
    transport, _ = fake_api(json_reply({"related": True, "numbers": []}, stop_reason=stop))
    with pytest.raises(LlmError) as e:
        ask(Llm("k", transport))
    assert e.value.reason == f"stop_{stop}"


def test_ask_json_reports_connection_failure_after_retries():
    transport, seen = fake_api(*[httpx2.ConnectError("x")] * 3)
    with pytest.raises(LlmError) as e:
        ask(Llm("k", transport))
    assert e.value.kind == "unavailable" and len(seen) == 3
