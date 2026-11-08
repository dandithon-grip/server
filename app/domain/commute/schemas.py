from datetime import time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.clients.odsay import TransitKind, TransitPath
from app.domain.commute.models import (
    AREA_MAX_LENGTH,
    EXTERNAL_ID_MAX_LENGTH,
    LINE_MAX_LENGTH,
    STATION_NAME_MAX_LENGTH,
    Preference,
    TransportMode,
)


class TransitStopInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1, max_length=STATION_NAME_MAX_LENGTH)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    external_id: str = Field(
        ...,
        min_length=1,
        max_length=EXTERNAL_ID_MAX_LENGTH,
        description="GET /transit/routes 응답의 external_id 그대로. 반대 방향 정류장을 구분한다",
    )


class TransitLegInput(BaseModel):
    """고른 대중교통 경로의 탑승 구간 하나.

    GET /transit/routes 의 legs 중 kind 가 walk 가 아닌 것.
    """

    model_config = ConfigDict(frozen=True)

    line: str = Field(..., min_length=1, max_length=LINE_MAX_LENGTH, description="노선")
    stops: list[TransitStopInput] = Field(
        ..., min_length=2, max_length=60, description="탑승 정류장부터 하차 정류장까지 순서대로"
    )


class SaveCommuteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_lat: float = Field(..., ge=-90, le=90, description="집 위도 (출근 출발)")
    from_lng: float = Field(..., ge=-180, le=180, description="집 경도 (출근 출발)")
    to_lat: float = Field(..., ge=-90, le=90, description="회사 위도 (출근 도착)")
    to_lng: float = Field(..., ge=-180, le=180, description="회사 경도 (출근 도착)")
    from_area: str = Field(..., min_length=1, max_length=AREA_MAX_LENGTH, description="집 동 이름")
    to_area: str = Field(..., min_length=1, max_length=AREA_MAX_LENGTH, description="회사 동 이름")
    go_time: time = Field(..., description="출근 시각 (HH:MM)")
    leave_time: time = Field(..., description="퇴근 시각 (HH:MM)")
    transport_mode: TransportMode = Field(..., description="walk | transit | car")
    line: str | None = Field(None, max_length=LINE_MAX_LENGTH, description="노선. transit 일 때")
    station_ids: list[UUID] = Field(
        default_factory=list,
        max_length=10,
        description="이용 정류장 id (데모 시드용). transit_legs 가 있으면 무시",
    )
    transit_legs: list[TransitLegInput] = Field(
        default_factory=list,
        max_length=5,
        description="GET /transit/routes 에서 고른 경로의 탑승 구간들. transit 이면 필수",
    )
    preference: Preference = Field(Preference.FULL, description="기본 취향 short | full | sunset")


class CommuteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float
    from_area: str
    to_area: str
    go_time: time
    leave_time: time
    transport_mode: TransportMode
    line: str | None
    station_ids: list[UUID]
    preference: Preference


class StationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line: str
    name: str
    lat: float
    lng: float
    order_no: int = Field(..., description="노선 안 순서. 0 부터")


class ParseTransitRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    odsay: dict[str, object] = Field(
        ...,
        description=(
            "프론트가 ODsay Web 키로 받은 searchPubTransPathT 응답 JSON 그대로. "
            "서버는 ODsay 를 부르지 않고 해석만 한다"
        ),
    )


class TransitStopResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    lat: float
    lng: float
    external_id: str = Field(..., description="ODsay stationID. 저장 때 그대로 보낸다")


class TransitLegResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: TransitKind
    line: str | None
    start_name: str | None
    end_name: str | None
    minutes: int
    stops: list[TransitStopResponse] = Field(
        ..., description="탑승 구간의 정류장. 도보는 비어 있음"
    )


class TransitPathResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    summary: str = Field(..., description='예: "대구 2호선 수성구청→반월당 · 도보 10분"')
    total_minutes: int
    transfers: int
    walk_minutes: int
    legs: list[TransitLegResponse]


class TransitRoutesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: list[TransitPathResponse] = Field(..., description="ODsay 순서 (빠른 순)")


def to_transit_path_response(path: TransitPath) -> TransitPathResponse:
    rides = [leg for leg in path.legs if leg.kind != TransitKind.WALK]
    parts = [f"{leg.line} {leg.start_name}→{leg.end_name}" for leg in rides]
    summary = " / ".join(parts) + f" · 도보 {path.walk_minutes}분"
    return TransitPathResponse(
        index=path.index,
        summary=summary,
        total_minutes=path.total_minutes,
        transfers=path.transfers,
        walk_minutes=path.walk_minutes,
        legs=[
            TransitLegResponse(
                kind=leg.kind,
                line=leg.line,
                start_name=leg.start_name,
                end_name=leg.end_name,
                minutes=leg.minutes,
                stops=[
                    TransitStopResponse(
                        name=s.name, lat=s.lat, lng=s.lng, external_id=s.external_id
                    )
                    for s in leg.stops
                ],
            )
            for leg in path.legs
        ],
    )
