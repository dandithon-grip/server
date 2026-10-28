from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ErrorCode(Enum):
    """비즈니스 에러 목록. HTTP 상태코드와 사용자에게 보여줄 메시지를 함께 정의한다."""

    INVALID_TOKEN = (401, "유효하지 않은 토큰입니다")
    USER_NOT_FOUND = (404, "사용자를 찾을 수 없습니다")
    COMMUTE_NOT_FOUND = (404, "출퇴근 정보가 없습니다")
    EXTERNAL_API_FAILED = (502, "외부 API 호출에 실패했습니다")
    LLM_FAILED = (502, "카드 생성에 실패했습니다")

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


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
