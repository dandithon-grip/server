from datetime import timedelta

from fastapi.security import HTTPAuthorizationCredentials
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ErrorCode
from app.core.security import JwksCache, get_optional_user
from app.domain.user.models import User
from app.domain.user.service import UserService
from tests.conftest import KeyPair, TokenFactory


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def count_users(session: AsyncSession, supabase_id: str) -> int:
    stmt = select(func.count()).select_from(User).where(User.supabase_id == supabase_id)
    return (await session.execute(stmt)).scalar_one()


# --- 401 -------------------------------------------------------------------


async def test_no_token_returns_401(client: AsyncClient) -> None:
    response = await client.get("/users/me")

    assert response.status_code == 401
    assert response.json() == {"message": ErrorCode.INVALID_TOKEN.message}


async def test_expired_token_returns_401(client: AsyncClient, make_token: TokenFactory) -> None:
    token = make_token(expires_in=timedelta(minutes=-5))

    response = await client.get("/users/me", headers=bearer(token))

    assert response.status_code == 401
    assert response.json() == {"message": ErrorCode.INVALID_TOKEN.message}


async def test_wrong_signature_returns_401(
    client: AsyncClient, make_token: TokenFactory, other_key_pair: KeyPair
) -> None:
    # 같은 kid, 다른 비밀키로 서명 → JWKS 의 공개키로 검증하면 실패해야 한다
    token = make_token(signer=other_key_pair)

    response = await client.get("/users/me", headers=bearer(token))

    assert response.status_code == 401


async def test_wrong_audience_returns_401(client: AsyncClient, make_token: TokenFactory) -> None:
    token = make_token(aud="anon")

    response = await client.get("/users/me", headers=bearer(token))

    assert response.status_code == 401


async def test_malformed_token_returns_401(client: AsyncClient) -> None:
    response = await client.get("/users/me", headers=bearer("not-a-jwt"))

    assert response.status_code == 401


# --- 200 -------------------------------------------------------------------


async def test_valid_token_creates_user_returns_200(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    sub = "22222222-2222-2222-2222-222222222222"
    assert await count_users(db_session, sub) == 0

    # 토큰에 nickname 없음 → 기본 닉네임
    response = await client.get("/users/me", headers=bearer(make_token(sub=sub)))

    assert response.status_code == 200
    body = response.json()
    assert body["nickname"] == "샛길러"
    assert body["city"] == get_settings().default_city
    assert body["notify_on"] is True
    assert await count_users(db_session, sub) == 1


async def test_token_with_nickname_creates_user_with_that_nickname(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    sub = "55555555-5555-5555-5555-555555555555"

    response = await client.get("/users/me", headers=bearer(make_token(sub=sub, nickname="승래")))

    assert response.status_code == 200
    assert response.json()["nickname"] == "승래"


async def test_long_nickname_is_truncated_to_column_length(
    client: AsyncClient, make_token: TokenFactory
) -> None:
    # nickname 컬럼은 50자. 더 길어도 500 이 아니라 잘라서 저장한다
    sub = "66666666-6666-6666-6666-666666666666"
    token = make_token(sub=sub, nickname="가" * 60)

    response = await client.get("/users/me", headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["nickname"] == "가" * 50


async def test_second_call_returns_same_user(
    client: AsyncClient, make_token: TokenFactory, db_session: AsyncSession
) -> None:
    sub = "33333333-3333-3333-3333-333333333333"
    headers = bearer(make_token(sub=sub))

    first = await client.get("/users/me", headers=headers)
    second = await client.get("/users/me", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert await count_users(db_session, sub) == 1


# --- get_optional_user (게스트 허용 엔드포인트용) ----------------------------


async def test_optional_user_without_token_returns_none(
    db_session: AsyncSession, jwks_cache: JwksCache
) -> None:
    user = await get_optional_user(
        credentials=None, jwks=jwks_cache, user_service=UserService(db_session)
    )

    assert user is None


async def test_optional_user_with_valid_token_returns_user(
    db_session: AsyncSession, jwks_cache: JwksCache, make_token: TokenFactory
) -> None:
    sub = "44444444-4444-4444-4444-444444444444"
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=make_token(sub=sub))

    user = await get_optional_user(
        credentials=credentials, jwks=jwks_cache, user_service=UserService(db_session)
    )

    assert user is not None
    assert user.supabase_id == sub
