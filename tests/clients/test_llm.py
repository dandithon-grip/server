"""LLM 클라이언트 기대 동작.

Messages API 응답 형식의 JSON 을 MockTransport 로 돌려준다. 네트워크 호출은 없다.
"""

import base64
import json
from collections.abc import Callable

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.clients.llm import LlmClient
from app.core.exceptions import AppException, ErrorCode

Handler = Callable[[httpx2.Request], httpx2.Response | Exception]


class Pick(BaseModel):
    model_config = ConfigDict(frozen=True)

    choice: int = Field(..., ge=0, le=2)
    text: str


def message_response(text: str, *, stop_reason: str = "end_turn") -> httpx2.Response:
    """Messages API 응답. 구조화 출력은 첫 text 블록에 JSON 문자열로 온다."""
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    return httpx2.Response(200, json=body)


class RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._handler(request)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(
    handler: Handler, api_key: str = "test-key"
) -> tuple[LlmClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    return LlmClient(api_key, "claude-opus-5-5", transport=transport), transport


# --- 정상 -------------------------------------------------------------------


async def test_complete_json_returns_schema_instance() -> None:
    client, transport = make_client(lambda _: message_response('{"choice": 1, "text": "노을"}'))

    result = await client.complete_json("골라", Pick, system="너는 선택기다")

    assert result == Pick(choice=1, text="노을")
    body = json.loads(transport.requests[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "너는 선택기다"
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "골라"}]}]
    assert body["output_config"]["effort"] == "low"

    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["choice", "text"]
    assert transport.requests[0].url.path == "/v1/messages"


async def test_complete_json_reply_carries_token_usage_and_latency() -> None:
    # 카드마다 토큰·응답 시간을 저장해 비용을 잰다 (#59)
    client, _ = make_client(lambda _: message_response('{"choice": 0, "text": "숲"}'))

    reply = await client.complete_json_reply("골라", Pick)

    assert reply.parsed == Pick(choice=0, text="숲")
    assert (reply.input_tokens, reply.output_tokens) == (10, 5)
    assert reply.latency_ms >= 0


async def test_vision_json_sends_base64_image_block() -> None:
    client, transport = make_client(lambda _: message_response('{"choice": 0, "text": "벚꽃"}'))

    result = await client.vision_json(b"\x89PNGfake", "image/png", "계절은?", Pick)

    assert result.text == "벚꽃"
    content = json.loads(transport.requests[0].content)["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"\x89PNGfake").decode("ascii"),
    }
    assert content[1] == {"type": "text", "text": "계절은?"}


# --- 실패 -------------------------------------------------------------------


async def test_empty_api_key_fails_without_calling() -> None:
    client, transport = make_client(lambda _: message_response("{}"), api_key="")

    with pytest.raises(AppException) as error:
        await client.complete_json("골라", Pick)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: message_response('{"choice": 7, "text": "범위 밖"}'),
        lambda _: message_response('{"text": "choice 없음"}'),
        lambda _: message_response("JSON 아님"),
        lambda _: message_response('{"choice": 1, "text": "x"}', stop_reason="refusal"),
        lambda _: message_response('{"choice": 1, "text": "x"}', stop_reason="max_tokens"),
    ],
    ids=["out-of-range", "missing-field", "not-json", "refusal", "max-tokens"],
)
async def test_schema_mismatch_and_stops_become_llm_failed(handler: Handler) -> None:
    client, _ = make_client(handler)

    with pytest.raises(AppException) as error:
        await client.complete_json("골라", Pick)

    assert error.value.code is ErrorCode.LLM_FAILED


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx2.Response(
            500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}}
        ),
        lambda _: httpx2.Response(
            401,
            json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}},
        ),
        lambda _: httpx2.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
        ),
        lambda _: httpx2.ReadTimeout("timeout"),
    ],
    ids=["500", "401", "429", "timeout"],
)
async def test_api_errors_become_external_api_failed(handler: Handler) -> None:
    client, _ = make_client(handler)

    with pytest.raises(AppException) as error:
        await client.complete_json("골라", Pick)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
