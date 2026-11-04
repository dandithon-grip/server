from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commute.models import Preference
from app.domain.route.models import LegType, PlaceType


class PointResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: float
    lng: float


class RouteResponse(BaseModel):
    """경로 하나. score·event_score 는 요청 때 취향·노을 창을 적용한 값."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    leg_type: LegType
    is_baseline: bool = Field(..., description="최단 경로(기준선)인지")
    name: str
    minutes: float = Field(..., description="소요 시간 (분)")
    add_minutes: float = Field(..., description="최단 대비 추가 시간 (분)")
    green_pct: float
    event_score: float
    score: float = Field(..., description="후보 순위 점수. 기준선은 0")
    tags: list[PlaceType]
    highlights: list[str] = Field(..., description="경유 지점 한 줄 설명")
    polyline: list[PointResponse]
    spots: list[UUID] = Field(..., description="경유 Place id")
    alight_station_id: UUID | None = Field(None, description="한 정거장 먼저일 때 하차역")


class RoutesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    preference: Preference = Field(..., description="적용한 취향")
    candidates: list[RouteResponse] = Field(..., description="후보 조건을 통과한 상위 3개, 점수순")
    shortest: list[RouteResponse] = Field(..., description="구간별 최단 경로")
