"""프론트(브라우저)가 API 를 부를 수 있게 CORS 가 켜져 있다."""

from httpx import AsyncClient

from app.core.config import get_settings


async def test_allowed_origin_gets_cors_headers(client: AsyncClient) -> None:
    origin = get_settings().cors_origin_list[0]

    response = await client.options(
        "/cards/demo",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


async def test_unknown_origin_gets_no_cors_headers(client: AsyncClient) -> None:
    response = await client.options(
        "/cards/demo",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers
