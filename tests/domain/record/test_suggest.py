"""F-11 비전 태그 제안 기대 동작.

- POST /records/suggest 는 Storage 다운로드 1회 + 비전 1회로 season·stage 를 제안한다
- Storage 에는 요청자의 토큰이 그대로 실리고, 403/404 는 404 IMAGE_NOT_FOUND 다
- 비전 실패·스키마 밖·비전이 못 읽는 사진 형식은 none 으로 응답한다 (사용자가 직접 고른다)
- 장소는 place_id 또는 현재 위치 100m 안의 Place 다. 좌표는 프롬프트에 넣지 않는다
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.llm import get_llm_client
from app.clients.supabase_storage import SupabaseStorageClient, get_storage_client
from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode
from app.core.geo import make_point
from app.domain.record.models import Season, Stage
from app.domain.record.schemas import VisionTags
from app.domain.record.service import detect_image_media_type
from app.domain.route.models import Place, PlaceType
from app.main import app
from tests.clients.conftest import RecordingTransport
from tests.conftest import TokenFactory

IMAGE_PATH = "11111111-1111-1111-1111-111111111111/2026-11-04/photo.jpg"
JPEG_BYTES = b"\xff\xd8\xff\xe0 fake jpeg"
PNG_BYTES = b"\x89PNG\r\n\x1a\n fake png"
GIF_BYTES = b"GIF89a fake gif"
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP fake webp"
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic fake heic"

PLACE_LAT = 35.8681
PLACE_LNG = 128.6005
# 위도 1도는 약 111km 다. 0.0005도 ≈ 55m, 0.002도 ≈ 222m
NEAR_LAT = PLACE_LAT + 0.0005
FAR_LAT = PLACE_LAT + 0.002


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class FakeStorage:
    """download 호출을 기록하는 가짜 Storage."""

    content: bytes = JPEG_BYTES
    error: ErrorCode | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def download(self, path: str, *, access_token: str) -> bytes:
        self.calls.append((path, access_token))
        if self.error is not None:
            raise AppException(self.error)
        return self.content


@dataclass
class VisionCall:
    image_bytes: bytes
    media_type: str
    prompt: str
    schema: type[BaseModel]


@dataclass
class FakeLlm:
    """vision_json 호출을 기록하는 가짜 비전."""

    tags: VisionTags = field(
        default_factory=lambda: VisionTags(season=Season.FOLIAGE, stage=Stage.PEAK)
    )
    error: ErrorCode | None = None
    calls: list[VisionCall] = field(default_factory=list)

    async def vision_json(
        self, image_bytes: bytes, media_type: str, prompt: str, schema: type[BaseModel]
    ) -> VisionTags:
        self.calls.append(VisionCall(image_bytes, media_type, prompt, schema))
        if self.error is not None:
            raise AppException(self.error)
        return self.tags


@pytest.fixture
def storage(client: AsyncClient) -> Iterator[FakeStorage]:
    fake = FakeStorage()
    app.dependency_overrides[get_storage_client] = lambda: fake
    yield fake


@pytest.fixture
def llm(client: AsyncClient) -> Iterator[FakeLlm]:
    fake = FakeLlm()
    app.dependency_overrides[get_llm_client] = lambda: fake
    yield fake


async def add_place(
    session: AsyncSession,
    *,
    name: str = "신천 동신교",
    lat: float = PLACE_LAT,
    lng: float = PLACE_LNG,
    city: str | None = None,
) -> Place:
    place = Place(
        name=name,
        location=make_point(lat, lng),
        types=[PlaceType.RIVER.value],
        description="물소리가 가까운 하천길",
        city=city or get_settings().default_city,
    )
    session.add(place)
    await session.flush()
    return place


def payload(**overrides: object) -> dict[str, object]:
    return {"image_path": IMAGE_PATH, **overrides}


# --- 인증 -------------------------------------------------------------------


async def test_guest_returns_401(client: AsyncClient, storage: FakeStorage, llm: FakeLlm) -> None:
    response = await client.post("/records/suggest", json=payload())

    assert response.status_code == 401
    assert storage.calls == []
    assert llm.calls == []


# --- 정상 -------------------------------------------------------------------


async def test_returns_vision_tags(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json() == {"season": "foliage", "stage": "peak", "place_id": None}


async def test_calls_storage_and_vision_once_each(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    token = make_token()

    await client.post("/records/suggest", json=payload(), headers=bearer(token))

    assert storage.calls == [(IMAGE_PATH, token)]
    assert len(llm.calls) == 1
    assert llm.calls[0].image_bytes == JPEG_BYTES
    assert llm.calls[0].media_type == "image/jpeg"
    assert llm.calls[0].schema is VisionTags


async def test_storage_request_carries_requester_token(
    client: AsyncClient, llm: FakeLlm, make_token: TokenFactory
) -> None:
    """엔드포인트부터 Storage 요청 헤더까지 실제 클라이언트로 이어서 확인한다 (#18 인수 조건)."""
    transport = RecordingTransport(lambda _: httpx.Response(200, content=JPEG_BYTES))
    real = SupabaseStorageClient("https://xxxx.supabase.co", "records", transport=transport)
    app.dependency_overrides[get_storage_client] = lambda: real
    token = make_token()

    response = await client.post("/records/suggest", json=payload(), headers=bearer(token))

    assert response.status_code == 200
    assert transport.requests[0].headers["authorization"] == f"Bearer {token}"


# --- Storage 실패 -----------------------------------------------------------


async def test_missing_image_returns_404_without_vision(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    storage.error = ErrorCode.IMAGE_NOT_FOUND

    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.IMAGE_NOT_FOUND.message}
    assert llm.calls == []


@pytest.mark.parametrize("status", [403, 404])
async def test_storage_refusal_returns_404_end_to_end(
    client: AsyncClient, llm: FakeLlm, make_token: TokenFactory, status: int
) -> None:
    transport = RecordingTransport(lambda _: httpx.Response(status, json={"error": "test"}))
    real = SupabaseStorageClient("https://xxxx.supabase.co", "records", transport=transport)
    app.dependency_overrides[get_storage_client] = lambda: real

    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.IMAGE_NOT_FOUND.message}


async def test_storage_outage_returns_502(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    """Storage 장애는 비전 실패가 아니다. none 으로 덮지 않는다."""
    storage.error = ErrorCode.EXTERNAL_API_FAILED

    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 502
    assert llm.calls == []


# --- 비전 폴백 --------------------------------------------------------------


@pytest.mark.parametrize("error", [ErrorCode.LLM_FAILED, ErrorCode.EXTERNAL_API_FAILED])
async def test_vision_failure_falls_back_to_none(
    client: AsyncClient,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
    error: ErrorCode,
) -> None:
    """LLM_FAILED 는 응답이 스키마 밖인 경우다. 둘 다 사용자가 직접 고르게 none 으로 준다."""
    llm.error = error

    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json() == {"season": "none", "stage": "none", "place_id": None}


async def test_unsupported_image_format_falls_back_without_vision(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    """비전이 못 읽는 형식(아이폰 HEIC 등)은 호출하지 않고 none 이다."""
    storage.content = HEIC_BYTES

    response = await client.post("/records/suggest", json=payload(), headers=bearer(make_token()))

    assert response.status_code == 200
    assert response.json()["season"] == "none"
    assert llm.calls == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (JPEG_BYTES, "image/jpeg"),
        (PNG_BYTES, "image/png"),
        (GIF_BYTES, "image/gif"),
        (WEBP_BYTES, "image/webp"),
        (HEIC_BYTES, None),
        (b"", None),
    ],
)
def test_detects_media_type_from_bytes(content: bytes, expected: str | None) -> None:
    assert detect_image_media_type(content) == expected


# --- 장소 -------------------------------------------------------------------


async def test_given_place_id_is_returned(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    place = await add_place(db_session)

    response = await client.post(
        "/records/suggest", json=payload(place_id=str(place.id)), headers=bearer(make_token())
    )

    assert response.json()["place_id"] == str(place.id)


async def test_unknown_place_returns_404_before_download(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    response = await client.post(
        "/records/suggest", json=payload(place_id=str(uuid4())), headers=bearer(make_token())
    )

    assert response.status_code == 404
    assert response.json() == {"message": ErrorCode.PLACE_NOT_FOUND.message}
    assert storage.calls == []
    assert llm.calls == []


async def test_place_within_100m_is_selected(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    place = await add_place(db_session)

    response = await client.post(
        "/records/suggest",
        json=payload(lat=NEAR_LAT, lng=PLACE_LNG),
        headers=bearer(make_token()),
    )

    assert response.json()["place_id"] == str(place.id)


async def test_nearest_of_two_places_is_selected(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    await add_place(db_session, name="조금 먼 곳", lat=PLACE_LAT - 0.0005)
    nearest = await add_place(db_session, name="가까운 곳", lat=PLACE_LAT + 0.0001)

    response = await client.post(
        "/records/suggest",
        json=payload(lat=PLACE_LAT, lng=PLACE_LNG),
        headers=bearer(make_token()),
    )

    assert UUID(response.json()["place_id"]) == nearest.id


async def test_place_beyond_100m_is_not_selected(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    await add_place(db_session)

    response = await client.post(
        "/records/suggest",
        json=payload(lat=FAR_LAT, lng=PLACE_LNG),
        headers=bearer(make_token()),
    )

    assert response.status_code == 200
    assert response.json()["place_id"] is None


async def test_place_in_other_city_is_not_selected(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    await add_place(db_session, city="other-city")

    response = await client.post(
        "/records/suggest",
        json=payload(lat=NEAR_LAT, lng=PLACE_LNG),
        headers=bearer(make_token()),
    )

    assert response.json()["place_id"] is None


async def test_coordinates_are_not_sent_to_vision(
    client: AsyncClient,
    db_session: AsyncSession,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
) -> None:
    await add_place(db_session)

    await client.post(
        "/records/suggest",
        json=payload(lat=NEAR_LAT, lng=PLACE_LNG),
        headers=bearer(make_token()),
    )

    assert str(NEAR_LAT) not in llm.calls[0].prompt
    assert str(PLACE_LNG) not in llm.calls[0].prompt


# --- 요청 검증 --------------------------------------------------------------


@pytest.mark.parametrize("coords", [{"lat": PLACE_LAT}, {"lng": PLACE_LNG}])
async def test_half_coordinates_return_422(
    client: AsyncClient,
    storage: FakeStorage,
    llm: FakeLlm,
    make_token: TokenFactory,
    coords: dict[str, float],
) -> None:
    response = await client.post(
        "/records/suggest", json=payload(**coords), headers=bearer(make_token())
    )

    assert response.status_code == 422
    assert storage.calls == []


async def test_image_path_outside_bucket_returns_422(
    client: AsyncClient, storage: FakeStorage, llm: FakeLlm, make_token: TokenFactory
) -> None:
    response = await client.post(
        "/records/suggest",
        json=payload(image_path="../other-user/photo.jpg"),
        headers=bearer(make_token()),
    )

    assert response.status_code == 422
    assert storage.calls == []
