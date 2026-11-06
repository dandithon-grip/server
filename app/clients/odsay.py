"""ODsay 대중교통 길찾기 클라이언트.

F-02 에서 출발·도착 좌표로 대중교통 경로 후보를 받는다. 후보마다 구간(지하철·버스·도보)과
탑승 구간의 정류장 목록(이름·좌표·순서)이 온다.
사용자가 하나 고르면 그 정류장들을 stations 에 저장한다.

엔드포인트: GET https://api.odsay.com/v1/api/searchPubTransPathT?SX=경도&SY=위도&EX&EY&apiKey
- 오류도 HTTP 200 에 {"error": ...} 로 온다. 이것도 EXTERNAL_API_FAILED
- 무료 키는 하루 30회. 테스트는 픽스처(tests/fixtures/odsay_search_path.json)로 돈다
"""

import logging
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.clients.tmap import LatLng
from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

ODSAY_SEARCH_URL = "https://api.odsay.com/v1/api/searchPubTransPathT"
TRAFFIC_SUBWAY = 1
TRAFFIC_BUS = 2
TRAFFIC_WALK = 3


class TransitKind(StrEnum):
    SUBWAY = "subway"
    BUS = "bus"
    WALK = "walk"


class TransitStop(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    lat: float
    lng: float
    external_id: str = Field(..., description="ODsay stationID")


class TransitLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: TransitKind
    line: str | None = Field(None, description='노선. 지하철 "대구 2호선", 버스 "309". 도보는 None')
    start_name: str | None = None
    end_name: str | None = None
    minutes: int = Field(..., ge=0)
    stops: list[TransitStop] = Field(
        default_factory=list, description="탑승 구간의 정류장. 도보는 비어 있다"
    )


class TransitPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    total_minutes: int
    transfers: int = Field(..., description="탑승 횟수 − 1")
    walk_minutes: int
    legs: list[TransitLeg]


class OdsayClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def search_transit(self, start: LatLng, end: LatLng) -> list[TransitPath]:
        """
        대중교통 경로 후보를 받는다.

        :raises AppException: EXTERNAL_API_FAILED - 키 없음·네트워크·4xx/5xx·error 객체·파싱 실패
        """
        # 1. 키 없으면 호출 전 차단
        if not self._api_key:
            logger.error("ODSAY_API_KEY 가 비어 있다")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)

        # 2. 호출
        params = {
            "SX": str(start.lng),
            "SY": str(start.lat),
            "EX": str(end.lng),
            "EY": str(end.lat),
            "apiKey": self._api_key,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(ODSAY_SEARCH_URL, params=params)
                if response.is_error:
                    logger.error("ODsay → %s %s", response.status_code, response.text[:200])
                response.raise_for_status()
                raw: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc

        # 3. 파싱
        return parse_search_response(raw)


def parse_search_response(raw: dict[str, Any]) -> list[TransitPath]:
    """
    searchPubTransPathT 응답을 TransitPath 목록으로.

    :raises AppException: EXTERNAL_API_FAILED - error 객체이거나 기대한 필드가 없음
    """
    if "error" in raw:
        logger.error("ODsay 오류 응답: %s", str(raw["error"])[:200])
        raise AppException(ErrorCode.EXTERNAL_API_FAILED)
    try:
        paths: list[TransitPath] = []
        for index, path in enumerate(raw["result"]["path"]):
            info = path["info"]
            legs = [_parse_leg(sub) for sub in path["subPath"]]
            rides = [leg for leg in legs if leg.kind != TransitKind.WALK]
            # info.totalWalkTime 은 -1(미제공)인 경우가 있어 도보 구간 합으로 센다
            walk_minutes = sum(leg.minutes for leg in legs if leg.kind == TransitKind.WALK)
            paths.append(
                TransitPath(
                    index=index,
                    total_minutes=int(info["totalTime"]),
                    transfers=max(len(rides) - 1, 0),
                    walk_minutes=walk_minutes,
                    legs=legs,
                )
            )
        return paths
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error("ODsay 응답 파싱 실패: %s", exc)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc


def _parse_leg(sub: dict[str, Any]) -> TransitLeg:
    traffic = int(sub["trafficType"])
    minutes = int(sub.get("sectionTime", 0))
    if traffic == TRAFFIC_WALK:
        return TransitLeg(kind=TransitKind.WALK, minutes=minutes)
    lane = sub["lane"][0]
    if traffic == TRAFFIC_SUBWAY:
        kind, line = TransitKind.SUBWAY, str(lane["name"])
    else:
        kind, line = TransitKind.BUS, str(lane["busNo"])
    stops = [
        TransitStop(
            name=str(station["stationName"]),
            lat=float(station["y"]),
            lng=float(station["x"]),
            external_id=str(station["stationID"]),
        )
        for station in sub["passStopList"]["stations"]
    ]
    return TransitLeg(
        kind=kind,
        line=line,
        start_name=str(sub["startName"]),
        end_name=str(sub["endName"]),
        minutes=minutes,
        stops=stops,
    )


def get_odsay_client() -> OdsayClient:
    return OdsayClient(get_settings().odsay_api_key)
