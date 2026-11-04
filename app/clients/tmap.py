"""TMAP 보행자 경로 API 래퍼.

F-03 이 걷는 구간의 기준선(1회)과 경유지별 후보 경로(≤6회)를 받을 때 쓴다.
service 는 httpx 를 직접 부르지 않고 이 클라이언트의 PedestrianRoute 만 본다.

엔드포인트: POST https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1
- 헤더 appKey, 본문 startX/startY/endX/endY (경도가 X, 위도가 Y)
- passList: "경도,위도_경도,위도" 최대 5곳
- 응답: GeoJSON FeatureCollection. 첫 Point feature(pointType=SP) 의 properties 에
  totalDistance(m)·totalTime(초), LineString feature 들의 coordinates 가 경로선
문서: https://tmap-skopenapi.readme.io/reference/보행자-경로안내
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.exceptions import AppException, ErrorCode

logger = logging.getLogger(__name__)

TMAP_PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian"
MAX_PASS_POINTS = 5
COORD_TYPE = "WGS84GEO"


class LatLng(BaseModel):
    """위도·경도 쌍. 외부 API 마다 순서가 달라서(TMAP 은 경도 먼저) 이름으로 구분한다."""

    model_config = ConfigDict(frozen=True)

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class PedestrianRoute(BaseModel):
    """service 가 쓰는 보행 경로. TMAP 응답에서 이 셋만 뽑는다."""

    model_config = ConfigDict(frozen=True)

    minutes: float = Field(..., description="총 소요 시간 (분)")
    distance_m: int = Field(..., description="총 거리 (m)")
    polyline: list[LatLng] = Field(..., min_length=2, description="경로선 좌표열")


class TmapClient:
    def __init__(
        self,
        app_key: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        :param transport: 테스트가 가짜 서버를 끼우는 자리. 운영에서는 None (실제 네트워크)
        """
        self._app_key = app_key
        self._timeout = timeout
        self._transport = transport

    async def pedestrian_route(
        self, start: LatLng, end: LatLng, pass_points: list[LatLng] | None = None
    ) -> PedestrianRoute:
        """
        보행자 경로를 받는다.

        :param start: 출발 좌표
        :param end: 도착 좌표
        :param pass_points: 경유지 (최대 5곳). 없으면 최단 보행 경로
        :return: 총 시간·거리·경로선
        :raises AppException: EXTERNAL_API_FAILED - 키 없음, 네트워크 오류, 4xx/5xx, 응답 파싱 실패
        :raises ValueError: 경유지가 5곳을 넘음 (호출하는 쪽의 버그)
        """
        # 1. 호출 전 검사. 키가 없으면 네트워크를 타지 않고 바로 실패한다
        if not self._app_key:
            logger.error("TMAP_APP_KEY 가 비어 있다")
            raise AppException(ErrorCode.EXTERNAL_API_FAILED)
        pass_points = pass_points or []
        if len(pass_points) > MAX_PASS_POINTS:
            raise ValueError(f"TMAP 경유지는 최대 {MAX_PASS_POINTS}곳이다")

        # 2. 요청 본문. TMAP 은 X=경도, Y=위도
        body: dict[str, str] = {
            "startX": str(start.lng),
            "startY": str(start.lat),
            "endX": str(end.lng),
            "endY": str(end.lat),
            "reqCoordType": COORD_TYPE,
            "resCoordType": COORD_TYPE,
            "startName": "start",
            "endName": "end",
        }
        if pass_points:
            body["passList"] = "_".join(f"{point.lng},{point.lat}" for point in pass_points)

        # 3. 호출. httpx 예외는 여기서 끝낸다
        raw = await self._post(body)

        # 4. 파싱
        return parse_pedestrian_response(raw)

    async def _post(self, body: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    TMAP_PEDESTRIAN_URL,
                    params={"version": "1"},
                    headers={"appKey": self._app_key},
                    json=body,
                )
                if response.is_error:
                    logger.error("TMAP 보행자 경로 → %s %s", response.status_code, response.text)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
        except (httpx.HTTPError, ValueError) as exc:
            raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc


def parse_pedestrian_response(raw: dict[str, Any]) -> PedestrianRoute:
    """
    GeoJSON FeatureCollection 을 PedestrianRoute 로 바꾼다.

    :raises AppException: EXTERNAL_API_FAILED - 기대한 필드가 없거나 형식이 다름
    """
    try:
        features: list[dict[str, Any]] = raw["features"]
        # 1. 총 시간·거리는 출발점(pointType=SP) Point 의 properties 에만 있다.
        #    위치를 믿지 않고 totalTime 이 있는 feature 를 찾는다
        summary = next(
            feature["properties"] for feature in features if "totalTime" in feature["properties"]
        )
        total_time_sec = float(summary["totalTime"])
        total_distance = int(summary["totalDistance"])

        # 2. LineString 들의 좌표를 이어 붙인다. TMAP 좌표는 [경도, 위도]
        polyline: list[LatLng] = []
        for feature in features:
            geometry = feature["geometry"]
            if geometry["type"] != "LineString":
                continue
            for lng, lat in geometry["coordinates"]:
                point = LatLng(lat=lat, lng=lng)
                if not polyline or polyline[-1] != point:
                    polyline.append(point)

        return PedestrianRoute(
            minutes=total_time_sec / 60, distance_m=total_distance, polyline=polyline
        )
    except (KeyError, IndexError, StopIteration, TypeError, ValueError) as exc:
        logger.error("TMAP 응답 파싱 실패: %s", exc)
        raise AppException(ErrorCode.EXTERNAL_API_FAILED) from exc


def get_tmap_client() -> TmapClient:
    return TmapClient(get_settings().tmap_app_key)
