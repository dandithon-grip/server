import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.clients.supabase_storage import is_safe_object_path
from app.domain.record.models import (
    IMAGE_PATH_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    Season,
    Stage,
)


def _inside_bucket(value: str) -> str:
    """버킷 밖을 가리키는 경로(`..`, 선행 `/`)는 어차피 내려받을 수 없다. 요청 단계에서 거른다."""
    if not is_safe_object_path(value):
        raise ValueError("버킷 안의 객체 경로여야 합니다")
    return value


ImagePath = Annotated[
    str,
    Field(
        min_length=1,
        max_length=IMAGE_PATH_MAX_LENGTH,
        description="Storage 객체 경로. 프론트가 올린 `{supabase user id}/...`",
    ),
    AfterValidator(_inside_bucket),
]


class SuggestRecordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_path: ImagePath
    place_id: UUID | None = Field(None, description="장소를 이미 골랐을 때. lat·lng 보다 우선한다")
    lat: float | None = Field(
        None, ge=-90, le=90, description="현재 위도. 100m 안 장소 자동 선택용"
    )
    lng: float | None = Field(None, ge=-180, le=180, description="현재 경도")

    @model_validator(mode="after")
    def _both_coordinates(self) -> Self:
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat 과 lng 은 함께 보내야 합니다")
        return self


class VisionTags(BaseModel):
    """비전 응답 형식. 이 스키마로 출력을 강제하고, 벗어나면 none 으로 폴백한다."""

    model_config = ConfigDict(frozen=True)

    season: Season
    stage: Stage


class RecordSuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    season: Season = Field(..., description="비전이 제안한 계절. 실패하면 none")
    stage: Stage = Field(..., description="비전이 제안한 단계. 실패하면 none")
    place_id: UUID | None = Field(None, description="장소. 100m 안에 없으면 null")


class CreateRecordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    place_id: UUID = Field(..., description="기록할 자연 지점 id")
    season: Season = Field(..., description="사용자가 고른 계절 태그. 비전 제안을 고칠 수 있다")
    stage: Stage = Field(..., description="사용자가 고른 단계")
    image_path: ImagePath
    ai_season: Season | None = Field(
        None, description="비전이 제안한 계절. 비전을 부르지 않았으면 생략한다"
    )
    ai_stage: Stage | None = Field(None, description="비전이 제안한 단계")
    title: str | None = Field(None, max_length=TITLE_MAX_LENGTH, description="한 줄 (선택)")
    date: datetime.date | None = Field(None, description="기록 날짜 (YYYY-MM-DD). 생략하면 오늘")


class RecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    place_id: UUID
    date: datetime.date
    season: Season
    stage: Stage
    ai_season: Season | None = Field(None, description="비전 제안. 부르지 않았으면 null")
    ai_stage: Stage | None = None
    title: str | None = None
    image_path: str
