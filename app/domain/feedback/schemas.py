from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.feedback.models import RATING_MAX, RATING_MIN


class CreateFeedbackRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_id: UUID = Field(..., description="걸은 경로 (내 동선의 경로)")
    rating: int = Field(
        ...,
        ge=RATING_MIN,
        le=RATING_MAX,
        description="별점 1~5. 태그 가중치 변화는 (별점 − 3) × 0.1",
    )


class PreferenceHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str = Field(..., description="4점 이상 평가가 3회 이상 쌓인 태그")
    message: str = Field(
        ..., description='마이 › 취향 설정에 보여줄 문구 ("요즘 하천길을 좋아하시네요")'
    )


class TagWeightsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    weights: dict[str, float] = Field(..., description="태그별 가중치 −1.0~+1.0")
    hint: PreferenceHint | None = Field(None, description="기본 취향 자동 제안. 없으면 null")


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_id: UUID
    date: date
    rating: int = Field(..., description="저장된 별점")
    weights: dict[str, float] = Field(..., description="갱신된 태그 가중치")
