from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

ERROR_SCHEMA_REF = "#/components/schemas/ErrorResponse"


class ErrorCode(Enum):
    """비즈니스 에러 목록. HTTP 상태코드와 사용자에게 보여줄 메시지를 함께 정의한다."""

    INVALID_TOKEN = (401, "유효하지 않은 토큰입니다")
    USER_NOT_FOUND = (404, "사용자를 찾을 수 없습니다")
    COMMUTE_NOT_FOUND = (404, "출퇴근 정보가 없습니다")
    ROUTES_NOT_READY = (404, "경로가 아직 없습니다. 샛길을 먼저 생성하세요")
    ROUTE_NOT_FOUND = (404, "경로를 찾을 수 없습니다")
    DEMO_NOT_READY = (404, "데모 동선이 준비되지 않았습니다")
    STATION_REQUIRED = (400, "대중교통은 정류장을 선택해야 합니다")
    STATION_NOT_FOUND = (400, "존재하지 않는 정류장입니다")
    PLACE_NOT_FOUND = (404, "존재하지 않는 장소입니다")
    IMAGE_NOT_FOUND = (404, "사진을 찾을 수 없습니다")
    EXTERNAL_API_FAILED = (502, "외부 API 호출에 실패했습니다")
    LLM_FAILED = (502, "카드 생성에 실패했습니다")

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


class ErrorResponse(BaseModel):
    """모든 에러 응답의 형식. 전역 핸들러가 이 모양으로만 내보낸다."""

    message: str = Field(
        ..., description="사용자에게 그대로 보여줄 메시지", examples=["유효하지 않은 토큰입니다"]
    )


class AppException(Exception):
    """비즈니스 예외. 도메인 코드는 이것만 던지고, 응답 변환은 전역 핸들러가 한다."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.message)
        self.code = code


def register_exception_handlers(app: FastAPI) -> None:
    """전역 예외 핸들러를 앱에 등록한다. 모든 에러 응답은 {"message": ...} 형식이다."""

    @app.exception_handler(AppException)
    async def handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.code.status == 401 else None
        return JSONResponse(
            status_code=exc.code.status, content={"message": exc.code.message}, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 기본 422 응답({"detail": [...]}) 대신 공통 형식으로 맞춘다.
        # 첫 번째 오류만 요약한다
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        return JSONResponse(
            status_code=422,
            content={"message": f"요청 값이 올바르지 않습니다 ({location}: {first['msg']})"},
        )


def use_error_schema_in_openapi(app: FastAPI) -> None:
    """Swagger 의 모든 4xx·5xx 응답에 ErrorResponse 스키마를 붙인다.

    라우터의 responses 는 설명만 적고, 형식은 여기서 한 번에 맞춘다.
    FastAPI 기본 422 스키마({"detail": [...]})는 실제 응답과 달라 명세에서 뺀다.
    """

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        # 1. 기본 명세 생성
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})

        # 2. 에러 응답을 전부 ErrorResponse 로
        components["ErrorResponse"] = ErrorResponse.model_json_schema()
        for operations in schema["paths"].values():
            for operation in operations.values():
                for code, response in operation["responses"].items():
                    if int(code) >= 400:
                        response["content"] = {
                            "application/json": {"schema": {"$ref": ERROR_SCHEMA_REF}}
                        }
                        if code == "422" and response.get("description") == "Validation Error":
                            response["description"] = "요청 값이 형식·범위에 맞지 않음"

        # 3. 쓰지 않게 된 기본 422 스키마 제거
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
