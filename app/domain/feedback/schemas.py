from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.feedback.models import Thumbs


class CreateFeedbackRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    route_id: UUID = Field(..., description="걸은 경로 (내 동선의 경로)")
    thumbs: Thumbs = Field(..., description="up | down")


class PreferenceHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str = Field(..., description="👍 3회 이상 누적된 태그")
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
    thumbs: Thumbs
    weights: dict[str, float] = Field(..., description="갱신된 태그 가중치")
