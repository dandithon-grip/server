from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.card.models import ReasonTag
from app.domain.commute.models import Preference
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
    route: RouteResponse | None = Field(None, description="고른 경로. 3·5단계는 없음")
    candidates: list[RouteResponse] = Field(..., description="다른 길 보기용 후보 ≤3, 점수순")
    shortest: list[RouteResponse] = Field(..., description="구간별 최단")
