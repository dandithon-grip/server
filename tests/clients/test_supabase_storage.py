"""Supabase Storage 다운로드 클라이언트 기대 동작.

서버는 service role 키를 런타임에 쓰지 않는다 (11/4 결정). 요청자의 access token 을 그대로
전달해서 버킷 RLS(본인 경로만 select)가 그대로 적용되게 한다.
그 헤더가 실제로 실리는지 MockTransport 로 확인하는 것이 이 파일의 핵심이다 (#18 인수 조건).
"""

import logging
from urllib.parse import quote

import httpx
import pytest

from app.clients.supabase_storage import SupabaseStorageClient
from app.core.exceptions import AppException, ErrorCode
from tests.clients.conftest import Handler, RecordingTransport

SUPABASE_URL = "https://xxxx.supabase.co"
BUCKET = "records"
TOKEN = "user-access-token"
IMAGE_PATH = "22222222-2222-2222-2222-222222222222/2026-11-04/photo.jpg"
IMAGE_BYTES = b"\xff\xd8\xff\xe0 fake jpeg bytes"


def make_client(
    handler: Handler, *, anon_key: str = ""
) -> tuple[SupabaseStorageClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    client = SupabaseStorageClient(SUPABASE_URL, BUCKET, anon_key=anon_key, transport=transport)
    return client, transport


def ok_handler(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=IMAGE_BYTES)


def status_handler(status: int) -> Handler:
    return lambda _: httpx.Response(status, json={"error": "test"})


# --- 토큰 전파 (인수 조건) --------------------------------------------------


async def test_download_sends_requester_bearer_token() -> None:
    client, transport = make_client(ok_handler)

    await client.download(IMAGE_PATH, access_token=TOKEN)

    assert transport.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_download_does_not_send_service_role_key() -> None:
    """클라이언트는 service role 키를 받지도, 보내지도 않는다. apikey 는 공개 anon 키뿐이다."""
    client, transport = make_client(ok_handler, anon_key="anon-public-key")

    await client.download(IMAGE_PATH, access_token=TOKEN)

    headers = transport.requests[0].headers
    assert headers["apikey"] == "anon-public-key"
    assert headers["authorization"] == f"Bearer {TOKEN}"


async def test_download_omits_apikey_when_not_configured() -> None:
    client, transport = make_client(ok_handler)

    await client.download(IMAGE_PATH, access_token=TOKEN)

    assert "apikey" not in transport.requests[0].headers


async def test_download_requests_authenticated_object_url() -> None:
    client, transport = make_client(ok_handler)

    await client.download(IMAGE_PATH, access_token=TOKEN)

    url = str(transport.requests[0].url)
    assert url == f"{SUPABASE_URL}/storage/v1/object/authenticated/{BUCKET}/{IMAGE_PATH}"


async def test_download_percent_encodes_path() -> None:
    """경로의 공백·#·?·한글은 URL 에서 인코딩된다.

    # 와 ? 가 fragment·query 로 새면 실제 파일이 있어도 다른 객체를 요청하게 된다.
    """
    client, transport = make_client(ok_handler)
    path = "22222222-2222-2222-2222-222222222222/사진 #1?.jpg"

    await client.download(path, access_token=TOKEN)

    url = transport.requests[0].url
    assert url.fragment == ""
    assert url.query == b""
    assert str(url) == (
        f"{SUPABASE_URL}/storage/v1/object/authenticated/{BUCKET}/{quote(path, safe='/')}"
    )


async def test_download_returns_file_bytes() -> None:
    client, _ = make_client(ok_handler)

    assert await client.download(IMAGE_PATH, access_token=TOKEN) == IMAGE_BYTES


# --- 실패 -------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404])
async def test_forbidden_or_missing_returns_image_not_found(status: int) -> None:
    """RLS 거절(403)과 없는 파일(404)은 같은 404 로 접는다. 비전 실패(502)와 구분된다."""
    client, _ = make_client(status_handler(status))

    with pytest.raises(AppException) as exc_info:
        await client.download(IMAGE_PATH, access_token=TOKEN)

    assert exc_info.value.code is ErrorCode.IMAGE_NOT_FOUND
    assert exc_info.value.code.status == 404


async def test_server_error_returns_external_api_failed() -> None:
    client, _ = make_client(status_handler(500))

    with pytest.raises(AppException) as exc_info:
        await client.download(IMAGE_PATH, access_token=TOKEN)

    assert exc_info.value.code is ErrorCode.EXTERNAL_API_FAILED


async def test_network_error_returns_external_api_failed() -> None:
    client, _ = make_client(lambda request: httpx.ConnectError("boom", request=request))

    with pytest.raises(AppException) as exc_info:
        await client.download(IMAGE_PATH, access_token=TOKEN)

    assert exc_info.value.code is ErrorCode.EXTERNAL_API_FAILED


async def test_network_error_log_omits_object_path(caplog: pytest.LogCaptureFixture) -> None:
    """httpx 예외 메시지에는 URL(= 사용자 경로)이 들어갈 수 있다. 로그에 그대로 남기지 않는다."""

    def handler(request: httpx.Request) -> Exception:
        return httpx.ConnectError(f"connection failed for {request.url}", request=request)

    client, _ = make_client(handler)

    with (
        caplog.at_level(logging.ERROR, logger="app.clients.supabase_storage"),
        pytest.raises(AppException),
    ):
        await client.download(IMAGE_PATH, access_token=TOKEN)

    assert IMAGE_PATH not in caplog.text


@pytest.mark.parametrize("path", ["", "/absolute.jpg", "../other-user/photo.jpg", "a/../../b.jpg"])
async def test_path_outside_bucket_is_not_requested(path: str) -> None:
    """경로는 요청 본문에서 온 값이다. 버킷을 벗어나는 경로는 호출 전에 404 로 막는다."""
    client, transport = make_client(ok_handler)

    with pytest.raises(AppException) as exc_info:
        await client.download(path, access_token=TOKEN)

    assert exc_info.value.code is ErrorCode.IMAGE_NOT_FOUND
    assert transport.requests == []


async def test_empty_token_is_not_requested() -> None:
    """토큰 없이 부르는 것은 호출하는 쪽의 버그다. 네트워크를 타지 않는다."""
    client, transport = make_client(ok_handler)

    with pytest.raises(AppException) as exc_info:
        await client.download(IMAGE_PATH, access_token="")

    assert exc_info.value.code is ErrorCode.INVALID_TOKEN
    assert transport.requests == []
