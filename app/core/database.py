from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import get_settings

# 제약조건·인덱스 이름 규칙. 이름이 정해져 있어야 alembic downgrade 가 제약을 정확히 찾아 지운다
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# 커넥션 풀. 만들 때는 연결하지 않고 첫 쿼리에서 연결한다
engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)

# expire_on_commit=False: commit 뒤에 엔티티 속성을 만료시키지 않는다.
# async 세션은 속성 접근 중 암묵적으로 DB 를 다시 읽을 수 없어서,
# True 면 commit 뒤 속성에 접근하는 순간 에러가 난다
session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """모든 엔티티의 부모. Base.metadata 에 테이블 정의가 모이고 alembic 이 이것을 본다."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at, updated_at 공통 컬럼. 값은 DB 가 채운다 (server_default)."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """요청 하나에 세션 하나를 열고, 응답이 끝나면 닫는다.

    yield 앞은 요청 전, 뒤는 응답 후에 실행된다. commit 은 하지 않는다 - service 의 책임이다.
    """
    async with session_factory() as session:
        yield session
