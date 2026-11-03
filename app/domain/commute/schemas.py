from datetime import time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commute.models import (
    AREA_MAX_LENGTH,
    LINE_MAX_LENGTH,
    Preference,
    TransportMode,
)


class SaveCommuteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_lat: float = Field(..., ge=-90, le=90, description="출발 위도")
    from_lng: float = Field(..., ge=-180, le=180, description="출발 경도")
    to_lat: float = Field(..., ge=-90, le=90, description="도착 위도")
    to_lng: float = Field(..., ge=-180, le=180, description="도착 경도")
    from_area: str = Field(
        ..., min_length=1, max_length=AREA_MAX_LENGTH, description="출발 동 이름"
    )
    to_area: str = Field(..., min_length=1, max_length=AREA_MAX_LENGTH, description="도착 동 이름")
    go_time: time = Field(..., description="출근 시각 (HH:MM)")
    leave_time: time = Field(..., description="퇴근 시각 (HH:MM)")
    transport_mode: TransportMode = Field(..., description="walk | transit | car")
    line: str | None = Field(None, max_length=LINE_MAX_LENGTH, description="노선. transit 일 때")
    station_ids: list[UUID] = Field(
        default_factory=list, max_length=10, description="이용 정류장 id. transit 이면 1개 이상"
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
