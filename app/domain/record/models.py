import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin

TITLE_MAX_LENGTH = 100
# Supabase Storage 의 객체 경로 ({supabase user id}/...). 버킷 이름은 포함하지 않는다
IMAGE_PATH_MAX_LENGTH = 300
TAG_MAX_LENGTH = 10


class Season(StrEnum):
    """계절 태그. 비전 AI 가 제안하고 사용자가 고칠 수 있다 (F-11)."""

    CHERRY = "cherry"
    FOLIAGE = "foliage"
    GINKGO = "ginkgo"
    SNOW = "snow"
    SUNSET = "sunset"
    NONE = "none"


class Stage(StrEnum):
    """계절의 단계. 비전 응답이 스키마 밖이면 NONE 으로 폴백한다 (F-11)."""

    PEAK = "peak"
    MID = "mid"
    END = "end"
    NONE = "none"


class Record(TimestampMixin, Base):
    """사용자가 남긴 기록 한 장.

    사진은 프론트가 Supabase Storage(비공개 버킷)에 올리고 여기엔 경로만 둔다.
    """

    __tablename__ = "records"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # RESTRICT: 큐레이션 지점을 지운다고 사용자 기록이 따라 사라지면 안 된다
    place_id: Mapped[UUID] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"), index=True)
    date: Mapped[datetime.date] = mapped_column(Date)
    # 사용자가 저장한 최종 값
    season: Mapped[Season] = mapped_column(String(TAG_MAX_LENGTH))
    stage: Mapped[Stage] = mapped_column(String(TAG_MAX_LENGTH))
    # 비전 AI 가 제안한 값. 비전을 부르지 않았으면 None
    # ("none" 은 비전이 답했지만 계절을 못 찾은 경우다 - 둘을 구분한다)
    ai_season: Mapped[Season | None] = mapped_column(String(TAG_MAX_LENGTH), nullable=True)
    ai_stage: Mapped[Stage | None] = mapped_column(String(TAG_MAX_LENGTH), nullable=True)
    title: Mapped[str | None] = mapped_column(String(TITLE_MAX_LENGTH), nullable=True)
    image_path: Mapped[str] = mapped_column(String(IMAGE_PATH_MAX_LENGTH))
