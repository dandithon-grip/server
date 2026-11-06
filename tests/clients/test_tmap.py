"""TMAP 보행자 경로 클라이언트 기대 동작.

실제 응답 JSON(tests/fixtures/tmap_pedestrian.json)을 MockTransport 로 돌려준다.
네트워크 호출은 없다.
"""

import json
from pathlib import Path

import httpx
import pytest

from app.clients.tmap import LatLng, PedestrianRoute, TmapClient
from app.core.exceptions import AppException, ErrorCode
from tests.clients.conftest import Handler, RecordingTransport

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tmap_pedestrian.json"

START = LatLng(lat=35.8581, lng=128.6305)
END = LatLng(lat=35.8651, lng=128.5935)
PASS_A = LatLng(lat=35.8620, lng=128.6100)
PASS_B = LatLng(lat=35.8640, lng=128.6000)


@pytest.fixture
def fixture_json() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def transport(fixture_json: dict[str, object]) -> RecordingTransport:
    return RecordingTransport(lambda _: httpx.Response(200, json=fixture_json))


@pytest.fixture
def client(transport: RecordingTransport) -> TmapClient:
    return TmapClient("test-app-key", transport=transport)


# --- 정상 -------------------------------------------------------------------


async def test_parses_minutes_distance_and_polyline(client: TmapClient) -> None:
    route = await client.pedestrian_route(START, END)

    assert isinstance(route, PedestrianRoute)
    assert route.minutes > 0
    assert route.distance_m > 0
    assert len(route.polyline) >= 2
    # 대구 범위. 위도·경도가 뒤바뀌면 여기서 걸린다
    assert all(35.0 < point.lat < 36.5 and 128.0 < point.lng < 129.5 for point in route.polyline)
    # 픽스처(실제 응답) 값: totalTime 3471초, totalDistance 3997m, 접점 중복을 뺀 좌표 186개
    assert route.minutes == pytest.approx(3471 / 60)
    assert route.distance_m == 3997
    assert len(route.polyline) == 186


async def test_request_has_key_and_lng_first_coordinates(
    client: TmapClient, transport: RecordingTransport
) -> None:
    await client.pedestrian_route(START, END)

    request = transport.requests[0]
    assert request.headers["appKey"] == "test-app-key"
    assert request.url.params["version"] == "1"
    body = json.loads(request.content)
    assert (body["startX"], body["startY"]) == ("128.6305", "35.8581")
    assert (body["endX"], body["endY"]) == ("128.5935", "35.8651")
    assert "passList" not in body


async def test_pass_points_become_pass_list(
    client: TmapClient, transport: RecordingTransport
) -> None:
    await client.pedestrian_route(START, END, pass_points=[PASS_A, PASS_B])

    body = json.loads(transport.requests[0].content)
    assert body["passList"] == "128.61,35.862_128.6,35.864"


async def test_more_than_five_pass_points_is_a_caller_bug(client: TmapClient) -> None:
    with pytest.raises(ValueError):
        await client.pedestrian_route(START, END, pass_points=[PASS_A] * 6)


# --- 실패 -------------------------------------------------------------------


async def test_empty_app_key_fails_without_calling(transport: RecordingTransport) -> None:
    client = TmapClient("")

    with pytest.raises(AppException) as error:
        await client.pedestrian_route(START, END)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, text="server error"),
        lambda _: httpx.Response(401, json={"error": "invalid key"}),
        lambda _: httpx.ReadTimeout("timeout"),
        lambda _: httpx.Response(200, text="not json"),
        lambda _: httpx.Response(200, json={"type": "FeatureCollection", "features": []}),
    ],
    ids=["500", "401", "timeout", "broken-json", "no-features"],
)
async def test_failures_become_external_api_failed(handler: Handler) -> None:
    client = TmapClient("test-app-key", transport=RecordingTransport(handler))

    with pytest.raises(AppException) as error:
        await client.pedestrian_route(START, END)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
