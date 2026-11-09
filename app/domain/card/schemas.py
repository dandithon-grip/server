from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.card.models import ReasonTag
from app.domain.commute.models import Preference
from app.domain.commute.schemas import RideResponse
from app.domain.route.models import ActionType
from app.domain.route.schemas import RouteResponse


class TodayCardResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    preference: Preference
    action_type: ActionType
    fallback_level: int = Field(..., ge=1, le=4)
    reason_tag: ReasonTag
    text: str = Field(..., description="카드 문장 (두 문장)")
    deviation: str | None = Field(None, description="AI 가 점수 1위와 다르게 고른 이유")
    by_llm: bool = Field(..., description="LLM 이 골랐으면 true, 템플릿 폴백이면 false")
    sunset_at: datetime
    sky: str | None
    air_grade: str | None
    ride_id: UUID | None = Field(
        None, description="어느 탑승 계획 기준 카드인지 (#90). null 이면 전체 계획을 합친 기본 카드"
    )
    route: RouteResponse | None = Field(None, description="고른 경로. 3·5단계는 없음")
    candidates: list[RouteResponse] = Field(..., description="다른 길 보기용 후보 ≤3, 점수순")
    shortest: list[RouteResponse] = Field(..., description="구간별 최단")
    rides: list[RideResponse] = Field(
        default_factory=list, description="동선의 탑승 계획들 (#88). 경로의 ride_id 가 가리킨다"
    )
