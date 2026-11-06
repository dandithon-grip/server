from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commute.models import Preference
from app.domain.route.models import ActionType, LegType, PlaceType

if TYPE_CHECKING:
    from app.domain.route.service import RankedRoute


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
    alight_station_id: UUID | None = Field(
        None, description="먼저 내리기(alight_early)일 때 내릴 정류장"
    )
    board_station_id: UUID | None = Field(
        None, description="늦게 타기(board_late)일 때 걸어가서 탈 정류장"
    )
    skip_stops: int = Field(0, description="먼저 내리기·늦게 타기로 건너뛴 정거장 수 (1~3)")


class RoutesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    preference: Preference = Field(..., description="적용한 취향")
    fallback_level: int = Field(..., ge=1, le=4, description="F-15 단계. 1 이면 샛길 후보 있음")
    action_type: ActionType = Field(..., description="오늘 카드의 행동 유형")
    candidates: list[RouteResponse] = Field(..., description="그 단계의 후보 ≤3, 점수순")
    shortest: list[RouteResponse] = Field(..., description="구간별 최단 경로")


def to_route_response(ranked: "RankedRoute") -> RouteResponse:
    """RankedRoute(엔티티 + 오늘 점수) 를 응답으로.

    엔티티 하나에는 score 가 없어 model_validate 를 못 쓴다.
    """
    route = ranked.route
    return RouteResponse(
        id=route.id,
        leg_type=route.leg_type,
        is_baseline=route.is_baseline,
        name=route.name,
        minutes=route.minutes,
        add_minutes=route.add_minutes,
        green_pct=route.green_pct,
        event_score=ranked.event_score,
        score=ranked.score,
        tags=ranked.tags,
        highlights=route.highlights,
        polyline=[PointResponse(lat=lat, lng=lng) for lat, lng in route.points],
        spots=route.spots,
        alight_station_id=route.alight_station_id,
        board_station_id=route.board_station_id,
        skip_stops=route.skip_stops,
    )
