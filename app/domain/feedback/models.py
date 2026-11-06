from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Date, ForeignKey, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin
from app.domain.route.models import PlaceType

# 규칙 조건값 (명세 F-16). 코드의 상수다
RATING_MIN = 1
RATING_MAX = 5
NEUTRAL_RATING = 3  # 3점은 가중치를 바꾸지 않는다
RATING_STEP = 0.1  # 별점 1점당 가중치 변화. 5점 +0.2, 1점 −0.2
WEIGHT_MIN = -1.0
WEIGHT_MAX = 1.0
HINT_MIN_RATING = 4  # 이 점수 이상을 "좋았다"로 센다
HINT_MIN_LIKES = 3
# 가중치가 붙는 태그. view 는 다리·전망 가산에만 쓰고 가중치 키는 아니다
FEEDBACK_TAGS = (
    PlaceType.RIVER,
    PlaceType.PARK,
    PlaceType.TREES,
    PlaceType.SUNSET,
    PlaceType.BRIDGE,
)


class Feedback(TimestampMixin, Base):
    """걷고 나서 별점 1~5. 같은 날 같은 경로에 다시 주면 덮어쓴다.

    경로는 동선을 다시 저장하면 새로 만들어진다. 그래도 평가 기록은 사용자 데이터라
    route_id 만 비우고 남기며, 평가 당시 태그를 함께 저장해 취향 제안에 계속 쓴다.
    """

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "route_id", "date", name="uq_feedback_user_route_date"),
        CheckConstraint(
            f"rating BETWEEN {RATING_MIN} AND {RATING_MAX}", name="ck_feedback_rating_range"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date)
    rating: Mapped[int] = mapped_column(SmallInteger)
    # 평가 당시 경로의 가중치 태그 (river·park·trees·sunset·bridge)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(10)), server_default="{}")
