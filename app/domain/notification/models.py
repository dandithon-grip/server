from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin

PUSH_TOKEN_MAX_LENGTH = 512


class PushToken(TimestampMixin, Base):
    """브라우저 하나의 FCM 등록 토큰 (#63). 사용자 하나가 여러 기기를 가질 수 있다.

    토큰은 기기(브라우저)마다 하나라 unique 다. 다른 계정으로 다시 등록하면 그 계정으로 옮긴다.
    """

    __tablename__ = "push_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(PUSH_TOKEN_MAX_LENGTH), unique=True)
