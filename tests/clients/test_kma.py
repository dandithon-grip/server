"""기상청 단기예보 클라이언트 기대 동작.

실제 응답 JSON(tests/fixtures/kma_vilage_fcst.json)을 MockTransport 로 돌려준다.
네트워크 호출은 없다.

격자 기대값은 기상청이 배포한 격자 좌표 엑셀
(기상청41_단기예보 조회서비스_오픈API활용가이드_격자_위경도(2610).xlsx) 의 행에서 가져왔다.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clients.kma import (
    KmaClient,
    Weather,
    WeatherSky,
    parse_forecast_response,
    to_grid,
)
from app.core.exceptions import AppException, ErrorCode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kma_vilage_fcst.json"
Handler = Callable[[httpx.Request], httpx.Response | Exception]

# 픽스처를 뜬 지점. 엑셀의 '대구광역시' 대표점 (격자 89,90)
DAEGU_LAT, DAEGU_LNG = 35.8685416666666, 128.603552777777


class RecordingTransport(httpx.AsyncBaseTransport):
    """요청을 기록하고 정해진 응답을 돌려주는 가짜 서버."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._handler(request)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fixture_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def transport(fixture_json: dict[str, Any]) -> RecordingTransport:
    return RecordingTransport(lambda _: httpx.Response(200, json=fixture_json))


@pytest.fixture
def client(transport: RecordingTransport) -> KmaClient:
    return KmaClient("test-service-key", transport=transport)


# --- 격자 변환 -------------------------------------------------------------
# 기상청 격자 엑셀의 행을 그대로 기대값으로 쓴다 (1단계/2단계, 경도(초/100)·위도(초/100) 열)


@pytest.mark.parametrize(
    ("place", "lat", "lng", "expected"),
    [
        ("대구광역시", 35.8685416666666, 128.603552777777, (89, 90)),
        ("대구광역시 중구", 35.8662861111111, 128.608397222222, (89, 90)),
        ("대구광역시 중구 삼덕동", 35.862575, 128.611166666666, (89, 90)),
        ("대구광역시 동구", 35.8836166666666, 128.637711111111, (90, 91)),
        ("대구광역시 동구 효목1동", 35.8781555555555, 128.647466666666, (90, 91)),
        ("서울특별시", 37.5635694444444, 126.980008333333, (60, 127)),
    ],
    ids=["daegu", "daegu-jung", "daegu-samdeok", "daegu-dong", "daegu-hyomok1", "seoul"],
)
def test_grid_matches_official_table(
    place: str, lat: float, lng: float, expected: tuple[int, int]
) -> None:
    grid = to_grid(lat, lng)

    assert (grid.nx, grid.ny) == expected, place


def test_grid_matches_guide_example() -> None:
    """활용가이드의 변환 예제: lon 126.929810, lat 37.488201 -> X 59, Y 125."""
    grid = to_grid(37.488201, 126.929810)

    assert (grid.nx, grid.ny) == (59, 125)


# --- 정상 -----------------------------------------------------------------


async def test_parses_sky_and_temperature(client: KmaClient) -> None:
    weather = await client.forecast(DAEGU_LAT, DAEGU_LNG, datetime(2026, 11, 4, 18, 0))

    # 픽스처(실제 응답) 2026-11-04 18시 예보: SKY 3(구름많음), PTY 0(없음), TMP 13
    assert isinstance(weather, Weather)
    assert weather.sky is WeatherSky.CLOUDY
    assert weather.temp_c == 13.0


async def test_request_carries_key_grid_and_latest_base_time(
    client: KmaClient, transport: RecordingTransport
) -> None:
    await client.forecast(DAEGU_LAT, DAEGU_LNG, datetime(2026, 11, 4, 18, 0))

    params = transport.requests[0].url.params
    assert params["serviceKey"] == "test-service-key"
    assert params["dataType"] == "JSON"
    assert (params["nx"], params["ny"]) == ("89", "90")
    # 18:00 에 가장 최근 발표는 17시 (발표 10분 뒤부터 제공)
    assert (params["base_date"], params["base_time"]) == ("20261104", "1700")


@pytest.mark.parametrize(
    ("at", "expected_base"),
    [
        (datetime(2026, 11, 4, 17, 5), ("20261104", "1400")),
        (datetime(2026, 11, 4, 17, 10), ("20261104", "1700")),
        (datetime(2026, 11, 4, 19, 40), ("20261104", "1700")),
        (datetime(2026, 11, 5, 0, 30), ("20261104", "2300")),
    ],
    ids=["before-publish", "at-publish", "evening", "after-midnight"],
)
async def test_base_time_is_the_latest_published_announcement(
    client: KmaClient, transport: RecordingTransport, at: datetime, expected_base: tuple[str, str]
) -> None:
    await client.forecast(DAEGU_LAT, DAEGU_LNG, at)

    params = transport.requests[0].url.params
    assert (params["base_date"], params["base_time"]) == expected_base


async def test_utc_datetime_is_read_in_kst(
    client: KmaClient, transport: RecordingTransport
) -> None:
    # 2026-11-04 09:00 UTC = 18:00 KST
    weather = await client.forecast(DAEGU_LAT, DAEGU_LNG, datetime(2026, 11, 4, 9, 0, tzinfo=UTC))

    params = transport.requests[0].url.params
    assert (params["base_date"], params["base_time"]) == ("20261104", "1700")
    assert weather.temp_c == 13.0


@pytest.mark.parametrize(
    ("at", "expected_temp"),
    [
        (datetime(2026, 11, 4, 19, 40), 12.0),
        (datetime(2026, 11, 4, 20, 0), 11.0),
        (datetime(2026, 11, 4, 17, 30), 13.0),
    ],
    ids=["within-hour", "exact-hour", "before-first-slot"],
)
async def test_uses_the_slot_at_or_before_the_target_time(
    client: KmaClient, at: datetime, expected_temp: float
) -> None:
    weather = await client.forecast(DAEGU_LAT, DAEGU_LNG, at)

    assert weather.temp_c == expected_temp


def test_precipitation_wins_over_sky(fixture_json: dict[str, Any]) -> None:
    """픽스처의 2026-11-05 17시 예보는 SKY 4(흐림) · PTY 1(비) 이다. 비가 하늘상태를 덮는다."""
    weather = parse_forecast_response(fixture_json, datetime(2026, 11, 5, 17, 0))

    assert weather.sky is WeatherSky.RAIN
    assert weather.temp_c == 14.0


# --- 결측 (예외가 아니다) ---------------------------------------------------


def test_empty_items_give_none_fields(fixture_json: dict[str, Any]) -> None:
    fixture_json["response"]["body"]["items"]["item"] = []

    weather = parse_forecast_response(fixture_json, datetime(2026, 11, 4, 18, 0))

    assert weather == Weather(sky=None, temp_c=None)


def test_nodata_result_code_gives_none_fields() -> None:
    """발표 시각 밖이면 NODATA_ERROR(03) 가 온다. 카드 생성을 막지 않는다."""
    raw = {"response": {"header": {"resultCode": "03", "resultMsg": "NODATA_ERROR"}}}

    weather = parse_forecast_response(raw, datetime(2026, 11, 4, 18, 0))

    assert weather == Weather(sky=None, temp_c=None)


def test_slot_without_sky_and_temp_gives_none_fields(fixture_json: dict[str, Any]) -> None:
    items = fixture_json["response"]["body"]["items"]["item"]
    fixture_json["response"]["body"]["items"]["item"] = [
        item for item in items if item["category"] not in ("SKY", "PTY", "TMP")
    ]

    weather = parse_forecast_response(fixture_json, datetime(2026, 11, 4, 18, 0))

    assert weather == Weather(sky=None, temp_c=None)


# --- 실패 -----------------------------------------------------------------


async def test_empty_service_key_fails_without_calling(transport: RecordingTransport) -> None:
    client = KmaClient("")

    with pytest.raises(AppException) as error:
        await client.forecast(DAEGU_LAT, DAEGU_LNG, datetime(2026, 11, 4, 18, 0))

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, text="server error"),
        lambda _: httpx.ReadTimeout("timeout"),
        lambda _: httpx.Response(200, text="not json"),
        # 등록되지 않은 키일 때 포털이 실제로 주는 응답 (403 + 에러 봉투)
        lambda _: httpx.Response(
            403,
            json={
                "OpenAPI_ServiceResponse": {
                    "cmmMsgHeader": {
                        "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
                        "returnAuthMsg": "등록되지 않은 서비스키",
                        "returnReasonCode": "30",
                    }
                }
            },
        ),
        # 요청 파라미터 오류는 200 으로 오기도 한다. 결측이 아니라 호출 실패다
        lambda _: httpx.Response(
            200,
            json={
                "response": {
                    "header": {
                        "resultCode": "10",
                        "resultMsg": "INVALID_REQUEST_PARAMETER_ERROR",
                    }
                }
            },
        ),
    ],
    ids=["500", "timeout", "broken-json", "unregistered-key", "invalid-parameter"],
)
async def test_failures_become_external_api_failed(handler: Handler) -> None:
    client = KmaClient("test-service-key", transport=RecordingTransport(handler))

    with pytest.raises(AppException) as error:
        await client.forecast(DAEGU_LAT, DAEGU_LNG, datetime(2026, 11, 4, 18, 0))

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
