from uuid import UUID, uuid4

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin

NICKNAME_MAX_LENGTH = 50


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Supabase Auth 의 user id (JWT 의 sub). 서버 로그인 식별자는 이것 하나다
    supabase_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(NICKNAME_MAX_LENGTH))
    city: Mapped[str] = mapped_column(String(20))
    notify_on: Mapped[bool] = mapped_column(default=True)
    # 둘러보기 카드(/cards/demo)의 주인. scripts/seed_demo.py 가 켠다
    is_demo: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
