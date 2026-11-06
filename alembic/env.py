"""Alembic 실행 환경.

- DB URL 은 Settings(.env) 에서 읽는다. 테스트는 `sqlalchemy.url` 옵션으로 덮어쓴다
- 모델은 여기서 import 해야 autogenerate 가 테이블을 인식한다. 도메인이 늘면 아래 import 에 추가한다
- geoalchemy2.alembic_helpers: spatial_ref_sys 같은 PostGIS 내부 테이블을 autogenerate 에서
  제외하고, Geometry 컬럼과 공간 인덱스를 올바르게 렌더링한다
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.domain.card.models  # noqa: F401  (TodayCard 를 metadata 에 등록)
import app.domain.commute.models  # noqa: F401  (Commute, Station 을 metadata 에 등록)
import app.domain.feedback.models  # noqa: F401  (Feedback 을 metadata 에 등록)
import app.domain.record.models  # noqa: F401  (Record 를 metadata 에 등록)
import app.domain.route.models  # noqa: F401  (Place 를 metadata 에 등록)
import app.domain.user.models  # noqa: F401  (User 를 metadata 에 등록)
from app.core.config import get_settings
from app.core.database import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: 기본값(True)이면 이미 만들어진 app.* 로거가 전부 꺼진다.
    # 테스트가 alembic 을 같은 프로세스에서 돌리므로 앱 로그 검증이 불가능해진다
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    """테스트가 넘긴 sqlalchemy.url 이 있으면 그것을, 없으면 Settings 의 DATABASE_URL 을 쓴다."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL 스크립트만 출력한다 (alembic upgrade head --sql)."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=alembic_helpers.include_object,
        render_item=alembic_helpers.render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=alembic_helpers.include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """async 엔진으로 연결한 뒤, 동기 API 인 alembic 을 run_sync 로 실행한다."""
    # postgis 도커 이미지는 search_path 에 tiger 스키마를 넣어 둔다. 그대로 두면 autogenerate 가
    # tiger 의 지오코더 테이블(addr, zip_state ...)을 우리 테이블로 보고 drop 하려 한다.
    # 접속 옵션으로 public 을 고정한다. (쿼리로 SET 하면 트랜잭션이 자동으로 열려
    # alembic 이 끝에 commit 을 건너뛰고, 마이그레이션이 통째로 롤백된다)
    engine = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"search_path": "public"}},
    )

    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
