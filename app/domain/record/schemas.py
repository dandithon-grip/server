import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clients.supabase_storage import is_safe_object_path
from app.domain.record.models import (
    IMAGE_PATH_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    Season,
    Stage,
)


class CreateRecordRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    place_id: UUID = Field(..., description="기록할 자연 지점 id")
    season: Season = Field(..., description="사용자가 고른 계절 태그. 비전 제안을 고칠 수 있다")
    stage: Stage = Field(..., description="사용자가 고른 단계")
    image_path: str = Field(
        ...,
        min_length=1,
        max_length=IMAGE_PATH_MAX_LENGTH,
        description="Storage 객체 경로. 프론트가 올린 `{supabase user id}/...`",
    )
    ai_season: Season | None = Field(
        None, description="비전이 제안한 계절. 비전을 부르지 않았으면 생략한다"
    )
    ai_stage: Stage | None = Field(None, description="비전이 제안한 단계")
    title: str | None = Field(None, max_length=TITLE_MAX_LENGTH, description="한 줄 (선택)")
    date: datetime.date | None = Field(None, description="기록 날짜 (YYYY-MM-DD). 생략하면 오늘")

    @field_validator("image_path")
    @classmethod
    def _inside_bucket(cls, value: str) -> str:
        """버킷 밖을 가리키는 경로(`..`, 선행 `/`)는 어차피 내려받을 수 없다. 저장 전에 거른다."""
        if not is_safe_object_path(value):
            raise ValueError("버킷 안의 객체 경로여야 합니다")
        return value


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
