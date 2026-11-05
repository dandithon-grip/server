from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.supabase_admin import AdminUser
from app.domain.user.models import User
from scripts.seed_demo import DEMO_NICKNAME, seed_demo

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"


@dataclass
class FakeSupabaseAdmin:
    """Supabase Admin API 대역. 네트워크 없이 계정 목록을 메모리에 두고 생성 호출을 기록한다."""

    accounts: dict[str, AdminUser] = field(default_factory=dict)
    create_calls: list[str] = field(default_factory=list)

    async def find_user_by_email(self, email: str) -> AdminUser | None:
        return self.accounts.get(email)

    async def create_user(self, *, email: str, password: str, nickname: str) -> AdminUser:
        self.create_calls.append(email)
        account = AdminUser(id=f"fake-{len(self.accounts) + 1}", email=email)
        self.accounts[email] = account
        return account


async def find_user(session: AsyncSession, supabase_id: str) -> User | None:
    stmt = select(User).where(User.supabase_id == supabase_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def count_users(session: AsyncSession, supabase_id: str) -> int:
    stmt = select(func.count()).select_from(User).where(User.supabase_id == supabase_id)
    return (await session.execute(stmt)).scalar_one()


async def test_account_missing_creates_account_and_user_row(db_session: AsyncSession) -> None:
    admin = FakeSupabaseAdmin()

    result = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert result.created_account is True
    assert admin.create_calls == [DEMO_EMAIL]
    user = await find_user(db_session, result.supabase_user_id)
    assert user is not None
    assert user.nickname == DEMO_NICKNAME
    assert user.is_demo is True  # 둘러보기 카드의 주인


async def test_account_exists_skips_creation_and_upserts_user_row(
    db_session: AsyncSession,
) -> None:
    existing = AdminUser(id="existing-account-id", email=DEMO_EMAIL)
    admin = FakeSupabaseAdmin(accounts={DEMO_EMAIL: existing})

    result = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert result.created_account is False
    assert admin.create_calls == []
    assert result.supabase_user_id == "existing-account-id"
    assert await find_user(db_session, "existing-account-id") is not None


async def test_running_twice_is_idempotent(db_session: AsyncSession) -> None:
    admin = FakeSupabaseAdmin()

    first = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)
    second = await seed_demo(admin, db_session, email=DEMO_EMAIL, password=DEMO_PASSWORD)

    assert first.supabase_user_id == second.supabase_user_id
    assert admin.create_calls == [DEMO_EMAIL]
    assert await count_users(db_session, first.supabase_user_id) == 1
