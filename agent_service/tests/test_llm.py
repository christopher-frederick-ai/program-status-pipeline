"""Exercise the real AnthropicLLM wrapper + SDK against a mocked HTTP transport."""

import json

import anthropic
import httpx2 as httpx
import pytest

from app.llm import AnthropicLLM, LLMError


def make_llm(handler) -> AnthropicLLM:
    client = anthropic.AsyncAnthropic(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return AnthropicLLM("sk-test", "claude-sonnet-5", 5.0, client=client)


OK_BODY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 3},
}


async def test_complete_parses_response_and_sends_expected_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    llm = make_llm(handler)
    res = await llm.complete(
        system="SYS", messages=[{"role": "user", "content": "hi"}], max_tokens=100
    )
    assert res.text == "Hello world"
    assert (res.input_tokens, res.output_tokens) == (12, 3)
    assert seen["body"]["system"] == "SYS"
    assert seen["body"]["model"] == "claude-sonnet-5"
    assert seen["body"]["max_tokens"] == 100


async def test_api_error_maps_to_502():
    def handler(request):
        return httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
        )

    with pytest.raises(LLMError) as ei:
        await make_llm(handler).complete(
            system="s", messages=[{"role": "user", "content": "x"}], max_tokens=10
        )
    assert ei.value.status_code == 502


async def test_timeout_maps_to_504():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as ei:
        await make_llm(handler).complete(
            system="s", messages=[{"role": "user", "content": "x"}], max_tokens=10
        )
    assert ei.value.status_code == 504
