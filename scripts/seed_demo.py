"""데모 계정 생성 스크립트.

    uv run python -m scripts.seed_demo

1. Supabase Admin API 로 DEMO_EMAIL 계정을 찾고, 없으면 만든다
   (user_metadata.nickname = "데모", 이메일 인증 완료 상태)
2. users 테이블에 그 계정의 행을 upsert 한다

SUPABASE_SERVICE_ROLE_KEY, DEMO_EMAIL, DEMO_PASSWORD 는 이 스크립트의 SeedSettings 만 읽는다.
앱의 Settings(app/core/config.py)에는 없다.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.supabase_admin import AdminUser, SupabaseAdminClient
from app.core.config import get_settings
from app.core.database import session_factory
from app.domain.user.service import UserService

DEMO_NICKNAME = "데모"


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_service_role_key: str = Field(..., min_length=1)
    demo_email: str = Field(..., min_length=1)
    demo_password: str = Field(..., min_length=6, description="Supabase 기본 최소 길이 6")


class SupabaseAdmin(Protocol):
    """seed_demo 가 필요로 하는 Admin API 동작. 테스트는 이 형태의 대역을 넣는다."""

    async def find_user_by_email(self, email: str) -> AdminUser | None: ...

    async def create_user(self, *, email: str, password: str, nickname: str) -> AdminUser: ...


@dataclass(frozen=True)
class SeedResult:
    supabase_user_id: str
    created_account: bool


async def seed_demo(
    admin: SupabaseAdmin,
    session: AsyncSession,
    *,
    email: str,
    password: str,
    nickname: str = DEMO_NICKNAME,
) -> SeedResult:
    """
    데모 계정과 users 행을 준비한다. 여러 번 실행해도 결과가 같다.

    :return: 계정의 Supabase user id 와 이번 실행에서 계정을 새로 만들었는지 여부
    :raises AppException: EXTERNAL_API_FAILED - Admin API 호출 실패
    """
    # 1. 계정 조회
    account = await admin.find_user_by_email(email)
    created = account is None

    # 2. 없으면 생성
    if account is None:
        account = await admin.create_user(email=email, password=password, nickname=nickname)

    # 3. users 행 upsert (이미 있으면 그대로 둔다)
    await UserService(session).get_or_create_user(supabase_id=account.id, nickname=nickname)

    return SeedResult(supabase_user_id=account.id, created_account=created)


async def main() -> None:
    seed_settings = SeedSettings()
    admin = SupabaseAdminClient(
        get_settings().supabase_url, seed_settings.supabase_service_role_key
    )

    async with session_factory() as session:
        result = await seed_demo(
            admin, session, email=seed_settings.demo_email, password=seed_settings.demo_password
        )

    state = "생성" if result.created_account else "이미 있음 (건너뜀)"
    print(f"데모 계정 {seed_settings.demo_email}: {state} / supabase_id={result.supabase_user_id}")


if __name__ == "__main__":
    asyncio.run(main())
