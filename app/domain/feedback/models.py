from datetime import date
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin
from app.domain.route.models import PlaceType

# 규칙 조건값 (명세 F-16). 코드의 상수다
WEIGHT_STEP = 0.2
WEIGHT_MIN = -1.0
WEIGHT_MAX = 1.0
HINT_MIN_UPS = 3
# 가중치가 붙는 태그. view 는 다리·전망 가산에만 쓰고 가중치 키는 아니다
FEEDBACK_TAGS = (
    PlaceType.RIVER,
    PlaceType.PARK,
    PlaceType.TREES,
    PlaceType.SUNSET,
    PlaceType.BRIDGE,
)


class Thumbs(StrEnum):
    UP = "up"
    DOWN = "down"


class Feedback(TimestampMixin, Base):
    """걷는 중 도착 시 👍/👎 1탭. 같은 날 같은 경로에 다시 누르면 덮어쓴다."""

    __tablename__ = "feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "route_id", "date", name="uq_feedback_user_route_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    route_id: Mapped[UUID] = mapped_column(ForeignKey("routes.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    thumbs: Mapped[str] = mapped_column(String(5))
