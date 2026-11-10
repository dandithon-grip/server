from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commute.models import Preference, RoutesState
from app.domain.commute.schemas import RideResponse
from app.domain.route.journey import Segment, SegmentKind, SegmentRole
from app.domain.route.models import ActionType, Direction, LegType, PlaceType

if TYPE_CHECKING:
    from app.domain.route.service import RankedRoute


class PointResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: float
    lng: float


class JourneySegmentResponse(BaseModel):
    """전체 여정의 조각 하나 (#77). 앞 조각의 끝 좌표 = 뒤 조각의 시작 좌표."""

    model_config = ConfigDict(frozen=True)

    kind: SegmentKind = Field(..., description="walk 걷기 · ride 탑승 · drive 자동차")
    role: SegmentRole = Field(
        ..., description="usual 평소 길 · saetgil 오늘 바뀐 구간(강조) · after_arrival 도착 후 산책"
    )
    polyline: list[PointResponse]
    minutes: float | None = Field(None, description="걷기 소요 분. 탑승·자동차는 없음")
    line: str | None = Field(None, description="탑승 노선")
    from_name: str | None = Field(None, description="탑승 정류장")
    to_name: str | None = Field(None, description="하차 정류장")


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
    drop_by: bool = Field(
        False, description="후보가 없어 가까운 자연 1곳에 잠깐 들렀다 오는 왕복 경로 (#116)"
    )
    ride_id: UUID | None = Field(
        None, description="어느 탑승 계획의 구간인지 (#88). rides[].id. 도보·자동차·도착 후는 null"
    )
    journey: list[JourneySegmentResponse] = Field(
        default_factory=list,
        description="이 경로를 골랐을 때의 출발 → 도착 전체 여정. polyline 은 그중 걷는 조각 하나",
    )


class RoutesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    preference: Preference = Field(..., description="적용한 취향")
    direction: Direction = Field(..., description="걷는 방향. to_home 퇴근(기본) · to_work 출근")
    fallback_level: int = Field(..., ge=1, le=4, description="F-15 단계. 1 이면 샛길 후보 있음")
    action_type: ActionType = Field(..., description="오늘 카드의 행동 유형")
    candidates: list[RouteResponse] = Field(..., description="그 단계의 후보 ≤3, 점수순")
    shortest: list[RouteResponse] = Field(..., description="구간별 최단 경로")
    rides: list[RideResponse] = Field(
        default_factory=list, description="동선의 탑승 계획들 (#88). 후보의 ride_id 가 가리킨다"
    )


def to_route_response(ranked: "RankedRoute", journey: list[Segment] | None = None) -> RouteResponse:
    """RankedRoute(엔티티 + 오늘 점수) 를 응답으로.

    엔티티 하나에는 score 가 없어 model_validate 를 못 쓴다.
    """
    route = ranked.route
    return RouteResponse(
        id=route.id,
        leg_type=ranked.leg_type,
        is_baseline=route.is_baseline,
        name=ranked.name,
        minutes=route.minutes,
        add_minutes=route.add_minutes,
        green_pct=route.green_pct,
        event_score=ranked.event_score,
        score=ranked.score,
        tags=ranked.tags,
        highlights=route.highlights,
        polyline=[PointResponse(lat=lat, lng=lng) for lat, lng in ranked.points],
        spots=route.spots,
        alight_station_id=ranked.alight_station_id,
        board_station_id=ranked.board_station_id,
        skip_stops=route.skip_stops,
        drop_by=ranked.drop_by,
        ride_id=route.ride_id,
        journey=[_segment_response(segment) for segment in journey or []],
    )


def _segment_response(segment: Segment) -> JourneySegmentResponse:
    return JourneySegmentResponse(
        kind=segment.kind,
        role=segment.role,
        polyline=[PointResponse(lat=lat, lng=lng) for lat, lng in segment.points],
        minutes=segment.minutes,
        line=segment.line,
        from_name=segment.from_name,
        to_name=segment.to_name,
    )


class GenerationStatusResponse(BaseModel):
    """샛길 생성 진행 상태 (#97)."""

    model_config = ConfigDict(frozen=True)

    state: RoutesState = Field(..., description="idle · pending · ready · failed")
    updated_at: datetime | None = Field(None, description="상태가 바뀐 시각")
    route_count: int = Field(..., description="저장된 경로 수 (기준선 포함)")
