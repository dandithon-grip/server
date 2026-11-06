"""Swagger 명세 기대 동작 (#46). 프론트가 /docs 와 /openapi.json 만 보고 연동할 수 있어야 한다."""

from typing import Any

from app.main import app

ERROR_REF = "#/components/schemas/ErrorResponse"


def operations() -> list[tuple[str, str, dict[str, Any]]]:
    spec = app.openapi()
    return [
        (method.upper(), path, op)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
    ]


def test_every_error_response_shows_message_schema() -> None:
    missing = [
        f"{method} {path} {code}"
        for method, path, op in operations()
        for code, response in op["responses"].items()
        if int(code) >= 400
        and response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
        != ERROR_REF
    ]

    assert missing == []


def test_error_schema_is_message_only() -> None:
    schema = app.openapi()["components"]["schemas"]["ErrorResponse"]

    assert schema["required"] == ["message"]
    assert list(schema["properties"]) == ["message"]


def test_default_validation_schema_is_not_advertised() -> None:
    # 전역 핸들러가 422 도 {"message"} 로 바꾼다.
    # FastAPI 기본 {"detail": [...]} 은 실제 응답과 달라 명세에 없어야 한다
    assert "HTTPValidationError" not in app.openapi()["components"]["schemas"]


def test_every_operation_has_description() -> None:
    missing = [f"{method} {path}" for method, path, op in operations() if not op.get("description")]

    assert missing == []


def test_top_description_explains_auth_and_errors() -> None:
    description = app.openapi()["info"]["description"]

    assert "Authorize" in description
    assert "/openapi.json" in description
    assert '"message"' in description
