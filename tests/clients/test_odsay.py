"""ODsay 대중교통 길찾기 클라이언트 기대 동작. 실제 응답 픽스처로 돈다."""

import json
from pathlib import Path

import httpx
import pytest

from app.clients.odsay import OdsayClient, TransitKind, parse_search_response
from app.clients.tmap import LatLng
from app.core.exceptions import AppException, ErrorCode
from tests.clients.conftest import Handler, RecordingTransport

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "odsay_search_path.json"
START = LatLng(lat=35.8575, lng=128.6370)
END = LatLng(lat=35.8690, lng=128.5960)


@pytest.fixture
def fixture_json() -> dict[str, object]:
    data: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_fixture_parses_subway_and_bus_paths(fixture_json: dict[str, object]) -> None:
    paths = parse_search_response(fixture_json)

    assert len(paths) == 17
    first = paths[0]
    assert first.total_minutes == 18
    assert first.transfers == 0
    assert [leg.kind for leg in first.legs] == [
        TransitKind.WALK,
        TransitKind.SUBWAY,
        TransitKind.WALK,
    ]
    subway = first.legs[1]
    assert subway.line == "대구 2호선"
    assert (subway.start_name, subway.end_name) == ("수성구청", "반월당")
    assert [s.name for s in subway.stops] == ["수성구청", "범어", "대구은행", "경대병원", "반월당"]
    assert subway.stops[0].lat == pytest.approx(35.8588, abs=1e-3)
    assert first.legs[0].stops == []  # 도보는 정류장 없음

    transfer = paths[1]
    assert transfer.transfers == 1
    assert [leg.line for leg in transfer.legs if leg.kind != TransitKind.WALK] == [
        "대구 2호선",
        "대구 1호선",
    ]

    bus = next(p for p in paths if any(leg.kind == TransitKind.BUS for leg in p.legs))
    bus_leg = next(leg for leg in bus.legs if leg.kind == TransitKind.BUS)
    assert bus_leg.line == "309"
    assert len(bus_leg.stops) == 11


async def test_request_sends_coordinates_and_key(fixture_json: dict[str, object]) -> None:
    transport = RecordingTransport(lambda _: httpx.Response(200, json=fixture_json))
    client = OdsayClient("test-key", transport=transport)

    paths = await client.search_transit(START, END)

    assert len(paths) == 17
    params = transport.requests[0].url.params
    assert (params["SX"], params["SY"]) == ("128.637", "35.8575")
    assert (params["EX"], params["EY"]) == ("128.596", "35.869")
    assert params["apiKey"] == "test-key"


async def test_empty_key_fails_without_calling() -> None:
    transport = RecordingTransport(lambda _: httpx.Response(200, json={}))
    client = OdsayClient("", transport=transport)

    with pytest.raises(AppException) as error:
        await client.search_transit(START, END)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(200, json={"error": {"code": "500", "message": "API KEY 오류"}}),
        lambda _: httpx.Response(200, json={"error": [{"code": "-8", "message": "no result"}]}),
        lambda _: httpx.Response(200, json={"result": {}}),
        lambda _: httpx.Response(500, text="server error"),
        lambda _: httpx.ReadTimeout("timeout"),
    ],
    ids=["error-object", "error-list", "missing-path", "500", "timeout"],
)
async def test_failures_become_external_api_failed(handler: Handler) -> None:
    client = OdsayClient("test-key", transport=RecordingTransport(handler))

    with pytest.raises(AppException) as error:
        await client.search_transit(START, END)

    assert error.value.code is ErrorCode.EXTERNAL_API_FAILED
