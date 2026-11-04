"""에어코리아 시도별 실시간 측정정보 클라이언트 기대 동작.

실제 응답 JSON(tests/fixtures/airkorea_ctprvn_rltm.json)을 MockTransport 로 돌려준다.
네트워크 호출은 없다.

픽스처는 대구 측정소 26곳이고, 그중 수창동은 pm10Value·pm25Value 가 '-' 이고
등급이 null 이다 (야간·점검 중 결측). 결측은 예외가 아니라 None 필드로 나간다.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clients.airkorea import AirGrade, AirKoreaClient, AirQuality, parse_pm_response
from app.core.exceptions import AppException, ErrorCode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "airkorea_ctprvn_rltm.json"
Handler = Callable[[httpx.Request], httpx.Response | Exception]

CITY = "daegu"


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
def client(transport: RecordingTransport) -> AirKoreaClient:
    return AirKoreaClient("test-service-key", transport=transport)


# --- 정상 -----------------------------------------------------------------


async def test_averages_urban_stations(client: AirKoreaClient) -> None:
    air = await client.pm(CITY)

    # 픽스처(실제 응답)의 도시대기 측정소 20곳 중 값이 있는 19곳 평균: pm10 67.1, pm25 30.0
    assert isinstance(air, AirQuality)
    assert air.pm10 == 67
    assert air.pm25 == 30
    assert air.grade is AirGrade.MODERATE


async def test_request_carries_key_and_sido_name(
    client: AirKoreaClient, transport: RecordingTransport
) -> None:
    await client.pm(CITY)

    params = transport.requests[0].url.params
    assert params["serviceKey"] == "test-service-key"
    assert params["returnType"] == "json"
    assert params["sidoName"] == "대구"


async def test_other_city_asks_for_its_own_sido(transport: RecordingTransport) -> None:
    client = AirKoreaClient("test-service-key", transport=transport)

    await client.pm("seoul")

    assert transport.requests[0].url.params["sidoName"] == "서울"


async def test_unknown_city_is_a_caller_bug(transport: RecordingTransport) -> None:
    client = AirKoreaClient("test-service-key", transport=transport)

    with pytest.raises(ValueError):
        await client.pm("atlantis")

    assert transport.requests == []


def test_roadside_stations_do_not_move_the_average(fixture_json: dict[str, Any]) -> None:
    """도로변대기는 값이 높게 치우쳐 도시 대표값에서 뺀다."""
    items = fixture_json["response"]["body"]["items"]
    urban_only = [item for item in items if item["mangName"] == "도시대기"]
    fixture_json["response"]["body"]["items"] = urban_only

    air = parse_pm_response(fixture_json)

    assert (air.pm10, air.pm25) == (67, 30)


def test_worst_station_grade_wins(fixture_json: dict[str, Any]) -> None:
    """도시 한쪽이 '나쁨'이면 도시 등급도 '나쁨'이다 (카드 문장이 '좋음'이라고 말하지 않게)."""
    items = fixture_json["response"]["body"]["items"]
    urban = [item for item in items if item["mangName"] == "도시대기"]
    urban[0]["pm10Grade"] = "3"

    air = parse_pm_response(fixture_json)

    assert air.grade is AirGrade.BAD


# --- 결측 (예외가 아니다) ---------------------------------------------------


def test_dash_and_null_stations_are_skipped(fixture_json: dict[str, Any]) -> None:
    items = fixture_json["response"]["body"]["items"]
    dash_station = next(item for item in items if item["pm10Value"] == "-")
    fixture_json["response"]["body"]["items"] = [dash_station]

    air = parse_pm_response(fixture_json)

    assert air == AirQuality(grade=None, pm10=None, pm25=None)


def test_empty_items_give_none_fields(fixture_json: dict[str, Any]) -> None:
    fixture_json["response"]["body"]["items"] = []

    air = parse_pm_response(fixture_json)

    assert air == AirQuality(grade=None, pm10=None, pm25=None)


def test_nodata_result_code_gives_none_fields() -> None:
    raw = {"response": {"header": {"resultCode": "03", "resultMsg": "No Data"}}}

    air = parse_pm_response(raw)

    assert air == AirQuality(grade=None, pm10=None, pm25=None)


def test_values_without_grades_still_give_values(fixture_json: dict[str, Any]) -> None:
    """등급만 null 인 측정소(픽스처의 본동)가 섞여도 값은 살린다."""
    items = fixture_json["response"]["body"]["items"]
    gradeless = next(
        item for item in items if item["pm10Grade"] is None and item["pm10Value"] not in (None, "-")
    )
    fixture_json["response"]["body"]["items"] = [gradeless]

    air = parse_pm_response(fixture_json)

    assert air.pm10 == int(gradeless["pm10Value"])
    assert air.grade is None


# --- 실패 -----------------------------------------------------------------


async def test_empty_service_key_fails_without_calling(transport: RecordingTransport) -> None:
    client = AirKoreaClient("")

    with pytest.raises(AppException) as error:
        await client.pm(CITY)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, text="server error"),
        lambda _: httpx.ReadTimeout("timeout"),
        lambda _: httpx.Response(200, text="not json"),
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
        lambda _: httpx.Response(
            200,
            json={"response": {"header": {"resultCode": "20", "resultMsg": "서비스 접근 거부"}}},
        ),
    ],
    ids=["500", "timeout", "broken-json", "unregistered-key", "access-denied"],
)
async def test_failures_become_external_api_failed(handler: Handler) -> None:
    client = AirKoreaClient("test-service-key", transport=RecordingTransport(handler))

    with pytest.raises(AppException) as error:
        await client.pm(CITY)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
