"""공통 픽스처.

- DB: DATABASE_URL 의 DB 이름 뒤에 _test 를 붙인 DB 를 쓴다 (docker/initdb 가 만든다).
  세션 시작 시 alembic upgrade head 로 스키마를 만들고, 테스트마다 트랜잭션을 롤백한다
- JWT: 세션마다 EC 키 쌍을 메모리에서 만들어 토큰을 서명하고, 앱의 JWKS 캐시를 그 공개키로
  바꿔치기한다. 네트워크 호출은 없다
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import ECAlgorithm
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import JwksCache, get_jwks_cache
from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_KID = "test-key-1"

TokenFactory = Callable[..., str]


# --- DB -------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(test_database_url: str) -> None:
    """테스트 DB 스키마를 alembic 으로 만든다. 마이그레이션 파일도 함께 검증된다."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # configparser 는 % 를 보간 문자로 쓰므로 이스케이프한다
    config.set_main_option("sqlalchemy.url", test_database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture
async def test_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    # asyncpg 커넥션은 이벤트 루프에 묶이고 pytest-asyncio 는 테스트마다 루프를 새로 만든다.
    # 그래서 엔진을 테스트마다 만들고 풀을 쓰지 않는다
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """테스트 하나 = 바깥 트랜잭션 하나.

    create_savepoint 모드에서는 service 의 session.commit() 이 savepoint 만 확정한다.
    테스트가 끝나면 바깥 트랜잭션을 롤백해 데이터를 남기지 않는다
    (Spring 의 @Transactional 테스트와 같다).
    """
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


# --- JWT -------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPair:
    private_key: ec.EllipticCurvePrivateKey
    kid: str

    def jwk(self) -> dict[str, object]:
        """공개키를 JWKS 의 키 하나(JWK) 형식으로 만든다."""
        public = ECAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        return {**public, "kid": self.kid, "alg": "ES256", "use": "sig"}


@pytest.fixture(scope="session")
def key_pair() -> KeyPair:
    """Supabase 역할을 하는 키 쌍. 공개키는 JWKS 에 올라가고 비밀키는 토큰을 서명한다."""
    return KeyPair(private_key=ec.generate_private_key(ec.SECP256R1()), kid=TEST_KID)


@pytest.fixture(scope="session")
def other_key_pair() -> KeyPair:
    """같은 kid 를 가진 다른 키. 서명 불일치 테스트용."""
    return KeyPair(private_key=ec.generate_private_key(ec.SECP256R1()), kid=TEST_KID)


@pytest.fixture
def jwks_cache(key_pair: KeyPair) -> JwksCache:
    cache = JwksCache(jwks_url="http://supabase.test/auth/v1/.well-known/jwks.json")
    cache.set_keys({"keys": [key_pair.jwk()]})
    return cache


@pytest.fixture
def make_token(key_pair: KeyPair) -> TokenFactory:
    """Supabase 가 발급하는 형식의 access token 을 만든다."""

    def _make(
        *,
        sub: str = "11111111-1111-1111-1111-111111111111",
        signer: KeyPair = key_pair,
        expires_in: timedelta = timedelta(hours=1),
        aud: str | None = None,
        nickname: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        payload: dict[str, object] = {
            "sub": sub,
            "aud": aud or get_settings().supabase_jwt_audience,
            "role": "authenticated",
            "iat": now,
            "exp": now + expires_in,
        }
        # 프론트가 signUp(options.data.nickname) 으로 넣은 값은 user_metadata 클레임으로 들어온다
        if nickname is not None:
            payload["user_metadata"] = {"nickname": nickname}
        return jwt.encode(
            payload, signer.private_key, algorithm="ES256", headers={"kid": signer.kid}
        )

    return _make


# --- HTTP ------------------------------------------------------------------


@pytest.fixture
async def client(db_session: AsyncSession, jwks_cache: JwksCache) -> AsyncIterator[AsyncClient]:
    """서버를 띄우지 않고 앱을 직접 호출하는 클라이언트 (MockMvc 에 해당).

    dependency_overrides 로 DB 세션과 JWKS 캐시를 테스트용으로 바꾼다 (@MockBean 에 해당).
    """

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_jwks_cache] = lambda: jwks_cache

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
