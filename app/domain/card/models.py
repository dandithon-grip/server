from datetime import date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin

TEXT_MAX_LENGTH = 120
DEVIATION_MAX_LENGTH = 200


class ReasonTag(StrEnum):
    """카드의 이유 칩. LLM 출력 스키마가 이 값으로 강제된다."""

    SUNSET = "sunset"
    RIVER = "river"
    PARK = "park"
    TREES = "trees"
    AIR = "air"
    SEASON = "season"


class TodayCard(TimestampMixin, Base):
    """오늘의 카드. 사용자·날짜당 1장 (데모는 퇴근 카드만, 11/3 결정)."""

    __tablename__ = "today_cards"
    # 사용자·날짜당 기본 카드 1장(ride_id NULL) + 탑승 계획별 카드 (#90). NULL 도 하나로 센다
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "date",
            "ride_id",
            name="uq_today_cards_user_date_ride",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date: Mapped[date] = mapped_column(Date)
    # 어느 탑승 계획 기준 카드인지 (#90). None 이면 전체 계획을 합쳐 고른 기본 카드
    ride_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("commute_rides.id", ondelete="CASCADE"), nullable=True
    )
    # 고른 경로. 3단계(늦추기·고개 돌리기)·5단계(없음)는 비어 있다
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(20))
    fallback_level: Mapped[int] = mapped_column(Integer)
    reason_tag: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(String(TEXT_MAX_LENGTH))
    # 점수 1위와 다르게 골랐을 때 LLM 이 적는 한 줄. 같으면 None
    deviation: Mapped[str | None] = mapped_column(String(DEVIATION_MAX_LENGTH), nullable=True)
    preference: Mapped[str] = mapped_column(String(10))
    # 그날의 조건 스냅샷
    sunset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sky: Mapped[str | None] = mapped_column(String(10), nullable=True)
    air_grade: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 1 이면 LLM 이 골랐고, 0 이면 템플릿 폴백
    by_llm: Mapped[bool] = mapped_column(default=False)
    # LLM 비용 (#59). 템플릿이면 None
    llm_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 퇴근 알림을 보낸 시각 (#63). 하루 한 번만 보내려고 둔다
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
