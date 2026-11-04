"""LLM 클라이언트 (Anthropic Claude).

F-05 카드(선택·이유·문장), F-11 기록(비전 계절 태그), F-17 자연어 취향이 쓴다.
두 메서드 모두 Pydantic 모델을 받아 그 스키마로 **구조화 출력을 강제**한다
(messages.parse + output_format). 문장 속 JSON 을 긁어내지 않는다.

실패 구분
- 네트워크·타임아웃·4xx/5xx        → AppException(EXTERNAL_API_FAILED)
- 응답이 스키마와 안 맞음·refusal   → AppException(LLM_FAILED)
호출하는 쪽(F-05)은 둘 다 받아 템플릿 문장으로 폴백한다.
"""

import base64
import logging
from typing import Any, Literal, TypeVar, cast

import anthropic
import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient, omit
from anthropic.types import MessageParam
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
ImageMediaType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]

MAX_OUTPUT_TOKENS = 2048
# 선택·분류·짧은 문장 생성이라 깊은 추론이 필요 없다. 비용·지연을 낮춘다
EFFORT: Literal["low"] = "low"


class LlmClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 20.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """
        :param transport: 테스트가 가짜 서버를 끼우는 자리. 운영에서는 None (실제 네트워크)
        """
        self._api_key = api_key
        self._model = model
        self._client = AsyncAnthropic(
            api_key=api_key or "missing",
            timeout=timeout,
            max_retries=1,
            http_client=DefaultAsyncHttpxClient(transport=transport) if transport else None,
        )

    async def complete_json(
        self, prompt: str, schema: type[SchemaT], *, system: str | None = None
    ) -> SchemaT:
        """
        텍스트 프롬프트 1회 호출. 응답은 schema 의 JSON 으로 강제된다.

        :param prompt: 사용자 메시지. 입력값(후보·날씨·장소 설명)을 전부 여기에 넣는다
        :param schema: 응답 형식. Pydantic 모델
        :param system: 역할·규칙. 요청마다 같은 문자열이면 캐시가 된다
        :return: schema 인스턴스
        :raises AppException: EXTERNAL_API_FAILED · LLM_FAILED
        """
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        return await self._parse(content, schema, system)

    async def vision_json(
        self,
        image_bytes: bytes,
        media_type: ImageMediaType,
        prompt: str,
        schema: type[SchemaT],
        *,
        system: str | None = None,
    ) -> SchemaT:
        """
        사진 1장 + 프롬프트 1회 호출. 응답은 schema 의 JSON 으로 강제된다.

        :param image_bytes: 사진 원본 바이트 (서버가 Storage 에서 받은 것)
        :param media_type: image/jpeg · image/png · image/webp · image/gif
        """
        encoded = base64.standard_b64encode(image_bytes).decode("ascii")
        content: list[dict[str, object]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": encoded},
            },
            {"type": "text", "text": prompt},
        ]
        return await self._parse(content, schema, system)

    async def _parse(
        self, content: list[dict[str, object]], schema: type[SchemaT], system: str | None
    ) -> SchemaT:
        # 1. 키 없으면 네트워크를 타지 않고 바로 실패
        if not self._api_key:
            logger.error("LLM_API_KEY 가 비어 있다")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)

        # 2. 호출. SDK 예외는 여기서 끝낸다
        message: MessageParam = {"role": "user", "content": cast(Any, content)}
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system if system is not None else omit,
                messages=[message],
                output_format=schema,
                output_config={"effort": EFFORT},
            )
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            logger.error("LLM 호출 실패: %s", exc)
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc
        except (ValidationError, ValueError) as exc:
            # SDK 가 응답 JSON 을 schema 로 검증하다 실패한 경우
            logger.error("LLM 응답이 스키마와 다르다: %s", exc)
            raise AppException(ErrorCode.LLM_FAILED) from exc

        # 3. 거절·중단·빈 결과는 스키마 실패로 본다
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            logger.error("LLM 응답 중단: stop_reason=%s", response.stop_reason)
            raise AppException(ErrorCode.LLM_FAILED)
        return response.parsed_output


def get_llm_client() -> LlmClient:
    settings = get_settings()
    return LlmClient(settings.llm_api_key, settings.llm_model)
